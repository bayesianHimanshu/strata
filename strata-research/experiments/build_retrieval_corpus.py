"""Build the Arm A open-book retrieval corpus — per-decision, molecule-scoped.

The first build was one undifferentiated blob: chunks carried no drug, so every
decision retrieved the same trials/labels (the wrong drug's label even surfaced), and
the PubMed arm silently returned nothing (esearch was called with retmax=0, so it
returned a count but no PMIDs). This rebuild fixes all three:

  * each decision gathers evidence scoped to ITS molecule(s) (sources.drug_identity),
    leakage-date-filtered to decision_date − buffer;
  * trials by `query.intr=molecule AND query.cond=indication` (not a global sweep);
  * label by `generic_name == molecule` EXACT — no default/first-doc fallback;
  * literature by `molecule AND indication AND (HTA terms)` with retmax set so PMIDs
    actually come back;
  * every chunk carries drug / indication / decision_id / doc_type / appraisal_id /
    doc_date, so retrieval is molecule-scoped at query time (the boundary's added
    predicate). NICE dossiers gain drug=molecule so same-drug siblings are retrievable
    while a decision's own dossier stays boundary-excluded.

After building, a fail-loud health gate (`assert_corpus_healthy`, reusing
introspect_retrieval) RAISES on a blob / wrong-drug routing / missing literature, so a
broken corpus can never silently produce a fake precision/recall delta again.

Pure converters + the gate are unit-tested; the network orchestration is a thin
wrapper over the typed clients (which carry the Phase 0 / WAF fixes).
"""
from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from core.config import LEAKAGE_BUFFER, SNAPSHOT_DIR
from core.contracts import Decision, DocType
from core.provenance import snapshot
from experiments import introspect_retrieval as introspect
from index.boundary import RetrievalBoundary, compute_sibling_map
from index.store import Chunk, InMemoryStore, chunk_from_dict, chunk_to_dict
from index.store import structure_aware_prefixed_chunks as _chunks
from sources.clinicaltrials import ClinicalTrialsClient, TrialRecord
from sources.drug_identity import DrugIdentity, normalize_drug
from sources.nice_guidance import GuidanceResult
from sources.openfda import LabelDoc, OpenFDAClient
from sources.pubmed import Abstract, PubMedClient

DEFAULT_CORPUS = Path("data/arm_a/corpus.jsonl")
NICE_CACHE = SNAPSHOT_DIR / "nice_guidance"

_HTA_CLAUSE = (
    '(cost-effectiveness OR "health technology assessment" OR comparator OR '
    '"overall survival" OR surrogate OR endpoint OR efficacy)'
)


class CorpusHealthError(RuntimeError):
    """Raised by the post-build gate when the corpus is a blob / mistargeted."""


# --------------------------------------------------------------------------- #
# Query hygiene (NICE free-text → parser-safe terms)
# --------------------------------------------------------------------------- #

_DASH_MAP = {ord(c): "-" for c in "‐‑‒–—―−"}


def clean_query(text: str, *, keep_hyphen: bool = True) -> str:
    """Sanitize for the CT.gov Essie / openFDA Lucene parsers (they 400 on en-dashes,
    parens, colons, slashes). Normalize unicode, map dashes, drop other specials."""
    import unicodedata

    t = unicodedata.normalize("NFKC", text).translate(_DASH_MAP)
    pattern = r"[^0-9A-Za-z \-]" if keep_hyphen else r"[^0-9A-Za-z ]"
    t = re.sub(pattern, " ", t)
    return re.sub(r"\s+", " ", t).strip()[:120]


def primary_generic(molecule: str) -> str:
    """A parser-safe generic-name term for openFDA."""
    return clean_query(molecule, keep_hyphen=False)


def pubmed_query(molecule: str, indication: str, *, with_indication: bool) -> str:
    mol = clean_query(molecule)
    if with_indication and indication.strip():
        return f'({mol}) AND ({clean_query(indication)}) AND {_HTA_CLAUSE}'
    return f'({mol}) AND {_HTA_CLAUSE}'


# --------------------------------------------------------------------------- #
# Retrievable doc + pure source→doc converters (drug/indication/decision_id tagged)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RetrievableDoc:
    source: str
    source_id: str
    url: str
    title: str
    text: str
    doc_date: date | None
    doc_type: DocType
    appraisal_id: str | None
    drug: str | None = None
    indication: str | None = None
    decision_id: str | None = None

    @property
    def content(self) -> bytes:
        return self.text.encode("utf-8")


def trial_to_doc(
    t: TrialRecord, *, drug=None, indication=None, decision_id=None
) -> RetrievableDoc:
    text = "\n".join(
        s
        for s in (
            f"Title: {t.title}",
            f"Conditions: {', '.join(t.conditions)}" if t.conditions else "",
            f"Phase: {t.phase}" if t.phase else "",
            f"Status: {t.overall_status}" if t.overall_status else "",
            ("Primary outcomes: " + "; ".join(t.primary_outcomes))
            if t.primary_outcomes
            else "",
        )
        if s
    )
    return RetrievableDoc(
        source="clinicaltrials.gov",
        source_id=t.nct_id or "NCT-unknown",
        url=f"https://clinicaltrials.gov/study/{t.nct_id}",
        title=t.title,
        text=text,
        doc_date=t.completion_date or t.start_date,
        doc_type=DocType.trial_registry,
        appraisal_id=None,
        drug=drug,
        indication=indication,
        decision_id=decision_id,
    )


def abstract_to_doc(
    a: Abstract, *, drug=None, indication=None, decision_id=None
) -> RetrievableDoc:
    return RetrievableDoc(
        source="pubmed",
        source_id=f"PMID:{a.pmid}",
        url=f"https://pubmed.ncbi.nlm.nih.gov/{a.pmid}/",
        title=a.title,
        text=f"Title: {a.title}\nAbstract: {a.abstract}",
        doc_date=a.pub_date,
        doc_type=DocType.literature,
        appraisal_id=None,
        drug=drug,
        indication=indication,
        decision_id=decision_id,
    )


def label_to_doc(
    label: LabelDoc, *, drug=None, indication=None, decision_id=None
) -> RetrievableDoc:
    name = label.brand or label.generic or drug or "drug"
    return RetrievableDoc(
        source="openfda",
        source_id=f"label:{drug or name}",
        url="https://api.fda.gov/drug/label.json",
        title=f"{name} label",
        text=label.text,
        doc_date=label.effective_date,
        doc_type=DocType.label,
        appraisal_id=None,
        drug=drug,
        indication=indication,
        decision_id=decision_id,
    )


def dossier_to_doc(
    gr: GuidanceResult, *, drug=None, indication=None
) -> RetrievableDoc | None:
    if gr.status != "ok" or gr.parsed is None:
        return None
    p = gr.parsed
    return RetrievableDoc(
        source="nice",
        source_id=f"{p.ta_id}:guidance",
        url=f"https://www.nice.org.uk/guidance/{p.ta_id.lower()}",
        title=p.title,
        text=p.rationale_raw,
        doc_date=p.published_date,
        doc_type=DocType.ta_final_guidance,  # gold-bearing → boundary excludes its own
        appraisal_id=p.ta_id,
        drug=drug,
        indication=indication or p.title,
        decision_id=p.ta_id,
    )


# --------------------------------------------------------------------------- #
# Indexing
# --------------------------------------------------------------------------- #


def index_doc(
    store: InMemoryStore,
    doc: RetrievableDoc,
    *,
    snapshot_root: Path = SNAPSHOT_DIR,
    seen: set[str] | None = None,
) -> int:
    """Snapshot + chunk + index one doc, tagging chunks with drug/indication/decision_id.
    Skips undated/empty docs and content already indexed (idempotent)."""
    if not doc.text.strip() or doc.doc_date is None:
        return 0
    rec = snapshot(
        doc.content,
        source=doc.source,
        source_id=doc.source_id,
        url=doc.url,
        doc_date=doc.doc_date,
        doc_type=doc.doc_type,
        appraisal_id=doc.appraisal_id,
        root=snapshot_root,
    )
    if seen is not None:
        if rec.content_sha256 in seen:
            return 0
        seen.add(rec.content_sha256)
    chunks = _chunks(
        doc.text,
        source_id=doc.source_id,
        doc_date=doc.doc_date,
        doc_title=doc.title,
        doc_type=doc.doc_type,
        appraisal_id=doc.appraisal_id,
        drug=doc.drug,
        indication=doc.indication,
        decision_id=doc.decision_id,
    )
    store.add(chunks)
    return len(chunks)


# --------------------------------------------------------------------------- #
# Fetch (network) — per molecule
# --------------------------------------------------------------------------- #


def load_cached_guidance(cache_dir: Path = NICE_CACHE) -> Iterator[GuidanceResult]:
    if not cache_dir.exists():
        return
    for path in sorted(cache_dir.glob("*.json")):
        yield GuidanceResult.model_validate_json(path.read_text())


def _lit_log(molecule: str, term: str, count: int, *, warn: bool = False) -> None:
    tag = "WARNING zero-literature" if warn else "literature esearch"
    print(
        f"[build_corpus] {tag} {molecule!r}: count={count}  q={term[:90]}",
        file=sys.stderr,
    )


def fetch_literature(
    molecule: str,
    indication: str,
    *,
    pubmed: PubMedClient,
    max_abstracts: int = 20,
    log=_lit_log,
) -> list[Abstract]:
    """esearch (retmax SET so PMIDs return) → efetch, via a relaxation chain.

    The first two attempts are the EXISTING queries (HTA term clause as a hard AND), so
    molecules that already land are untouched and the rebuild stays idempotent for them.
    Newer drugs with no HEOR papers yet (talazoparib, epcoritamab) fall through to the
    new fallbacks: the HTA clause becomes a soft signal (dropped), and finally molecule
    ALONE, recency-capped (sort=pub_date), so clinical literature — endpoints, survival,
    trial design, the categories actually in play — is still gathered. The raw esearch
    count is logged per molecule; a final 0 emits a WARNING (silent 0 hid this twice)."""
    mol, ind = clean_query(molecule), clean_query(indication)
    attempts: list[tuple[str, int, str | None]] = []
    if ind:
        attempts.append((pubmed_query(molecule, indication, with_indication=True),
                         max_abstracts, None))
    attempts.append((pubmed_query(molecule, indication, with_indication=False),
                     max_abstracts, None))
    if ind:  # NEW: drop the HTA hard-AND (soft boost, not a MUST)
        attempts.append((f"({mol}) AND ({ind})", max_abstracts, None))
    attempts.append((f"({mol})", max(max_abstracts, 40), "pub_date"))  # molecule alone

    for term, retmax, sort in attempts:
        res, _ = pubmed.search(term, retmax=retmax, sort=sort)
        log(molecule, term, res.count or 0)
        if res.pmids:
            abstracts, _ = pubmed.fetch_abstracts(res.pmids[:retmax])
            return abstracts
    log(molecule, f"({mol})", 0, warn=True)
    return []


def fetch_molecule_docs(
    molecule: str,
    indication: str,
    decision_id: str,
    *,
    ct: ClinicalTrialsClient,
    pubmed: PubMedClient,
    fda: OpenFDAClient,
    max_trials: int = 20,
    max_abstracts: int = 20,
) -> list[RetrievableDoc]:
    """All public evidence for ONE molecule + indication. Each source is independent;
    a source failure is logged and skipped (a partial corpus is recoverable)."""
    docs: list[RetrievableDoc] = []
    tag = {"drug": molecule, "indication": indication, "decision_id": decision_id}

    try:
        trials, _, _ = ct.search(
            condition=clean_query(indication),
            intervention=clean_query(molecule),
            status=None,
            page_size=max_trials,
        )
        docs += [trial_to_doc(t, **tag) for t in trials]
    except Exception as exc:  # noqa: BLE001 - one source must not kill the run
        _warn(decision_id, f"clinicaltrials[{molecule}]", exc)

    try:
        for a in fetch_literature(
            molecule, indication, pubmed=pubmed, max_abstracts=max_abstracts
        ):
            docs.append(abstract_to_doc(a, **tag))
    except Exception as exc:  # noqa: BLE001
        _warn(decision_id, f"pubmed[{molecule}]", exc)

    try:
        # EXACT generic match; if none, parse_label_docs returns [] → NO label (no
        # default-doc fallback — that was the ORSERDU wrong-drug bug).
        labels, _ = fda.fetch_label_docs(
            f'openfda.generic_name:"{primary_generic(molecule)}"'
        )
        docs += [label_to_doc(label, **tag) for label in labels]
    except Exception as exc:  # noqa: BLE001
        _warn(decision_id, f"openfda[{molecule}]", exc)
    return docs


def _warn(decision_id: str, source: str, exc: Exception) -> None:
    print(
        f"[build_corpus] {decision_id}: {source} fetch failed "
        f"({type(exc).__name__}: {exc}) — skipping that source",
        file=sys.stderr,
    )


# --------------------------------------------------------------------------- #
# Assemble
# --------------------------------------------------------------------------- #


def _drug_from_title(title: str) -> DrugIdentity:
    """Recover a molecule from a NICE guidance title ('Drug for indication')."""
    head = re.split(r"\bfor\b", title, maxsplit=1, flags=re.IGNORECASE)[0]
    return normalize_drug(head)


def build_corpus(
    decisions: Iterable[Decision],
    *,
    ct: ClinicalTrialsClient,
    pubmed: PubMedClient,
    fda: OpenFDAClient,
    nice_cache_dir: Path = NICE_CACHE,
    snapshot_root: Path = SNAPSHOT_DIR,
    buffer: timedelta = LEAKAGE_BUFFER,
) -> list[Chunk]:
    """NICE dossiers (global, molecule-tagged) + per-decision per-molecule public
    evidence, each date-filtered to the decision's leakage cutoff."""
    decisions = list(decisions)
    store = InMemoryStore()
    seen: set[str] = set()
    by_ta = {d.decision_id: d for d in decisions}

    for gr in load_cached_guidance(nice_cache_dir):
        owner = by_ta.get(gr.ta_id)
        di = normalize_drug(owner.drug or "") if owner else _drug_from_title(
            gr.parsed.title if gr.parsed else ""
        )
        doc = dossier_to_doc(
            gr,
            drug=di.primary or None,
            indication=owner.indication if owner else None,
        )
        if doc is not None:
            index_doc(store, doc, snapshot_root=snapshot_root, seen=seen)

    for d in decisions:
        di = normalize_drug(d.drug or "")
        cutoff = d.decision_date - buffer
        for molecule in sorted(di.molecules):
            for doc in fetch_molecule_docs(
                molecule, d.indication, d.decision_id, ct=ct, pubmed=pubmed, fda=fda
            ):
                if doc.doc_date is None or doc.doc_date >= cutoff:
                    continue  # not strictly before this decision's cutoff
                index_doc(store, doc, snapshot_root=snapshot_root, seen=seen)
    return store.chunks


# --------------------------------------------------------------------------- #
# Fail-loud post-build health gate
# --------------------------------------------------------------------------- #


def assert_corpus_healthy(
    decisions: list[Decision],
    chunks: list[Chunk],
    *,
    buffer_days: int = 90,
    min_distinct_ratio: float = 0.4,
    min_literature_ratio: float = 0.5,
    min_drug_match: float = 0.8,
    probe_query: str = "comparator overall survival ICER endpoint surrogate",
    k: int = 8,
) -> dict:
    """RAISE CorpusHealthError unless the corpus is decision-specific (not a blob),
    literature landed, and retrieval routes to the right molecule. Returns a report."""
    n = len(decisions)
    dicts = [chunk_to_dict(c) for c in chunks]
    comp = introspect.composition(dicts)

    if comp["distinct_drugs"] <= 1 or comp["distinct_drugs"] < min_distinct_ratio * n:
        raise CorpusHealthError(
            f"blob: distinct_drugs={comp['distinct_drugs']} not ≈ n_decisions={n} "
            f"(min {min_distinct_ratio:.0%}) — chunks are not molecule-specific"
        )
    if comp["literature_chunks"] == 0:
        raise CorpusHealthError(
            "literature arm did not land (0 literature chunks) — check the PubMed query"
        )

    store = InMemoryStore()
    store.add(chunks)
    sib = compute_sibling_map(decisions)

    pool_sigs: set[frozenset] = set()
    lit_present = 0
    matched = total = 0
    for d in decisions:
        di = normalize_drug(d.drug or "")
        boundary = RetrievalBoundary.for_decision(
            d,
            buffer=timedelta(days=buffer_days),
            sibling_appraisal_ids=sib.get(d.appraisal_id or d.decision_id, frozenset()),
            molecules=di.molecules,
        )
        eligible = boundary.filter(store.chunks)
        pool_sigs.add(frozenset((c.source_id, c.span.start) for c in eligible))
        if any(c.doc_type == DocType.literature for c in eligible):
            lit_present += 1
        for hit in store.search(probe_query, boundary=boundary, k=k):
            total += 1
            if hit.chunk.drug in di.molecules:
                matched += 1

    if n > 1 and len(pool_sigs) <= 1:
        raise CorpusHealthError(
            "blob: every decision has an identical eligible pool — retrieval is not "
            "decision-specific"
        )
    if lit_present < min_literature_ratio * n:
        raise CorpusHealthError(
            f"literature present for only {lit_present}/{n} decisions "
            f"(min {min_literature_ratio:.0%})"
        )
    drug_match = (matched / total) if total else 0.0
    if total and drug_match < min_drug_match:
        raise CorpusHealthError(
            f"wrong-drug routing: only {drug_match:.0%} of top-{k} retrieved chunks "
            f"match the decision's molecule (min {min_drug_match:.0%})"
        )

    return {
        "n_decisions": n,
        "distinct_drugs": comp["distinct_drugs"],
        "distinct_eligible_pools": len(pool_sigs),
        "literature_chunks": comp["literature_chunks"],
        "decisions_with_literature": lit_present,
        "retrieved_drug_match_rate": round(drug_match, 3),
        "by_doc_type": comp["by_doc_type"],
    }


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def save_corpus(chunks: list[Chunk], path: Path = DEFAULT_CORPUS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for c in chunks:
            fh.write(json.dumps(chunk_to_dict(c)) + "\n")


def load_corpus(path: Path = DEFAULT_CORPUS) -> InMemoryStore:
    store = InMemoryStore()
    with Path(path).open() as fh:
        store.add([chunk_from_dict(json.loads(line)) for line in fh if line.strip()])
    return store


def load_decisions(path: str | Path) -> list[Decision]:
    raw = json.loads(Path(path).read_text())
    records = raw.values() if isinstance(raw, dict) else raw
    return [Decision.model_validate(r) for r in records]


def _main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Build the Arm A retrieval corpus")
    ap.add_argument("--decisions", default="data/arm_a/decisions.json")
    ap.add_argument("--out", type=Path, default=DEFAULT_CORPUS)
    ap.add_argument("--buffer-days", type=int, default=LEAKAGE_BUFFER.days)
    args = ap.parse_args()

    decisions = load_decisions(args.decisions)
    chunks = build_corpus(
        decisions,
        ct=ClinicalTrialsClient(),
        pubmed=PubMedClient(),
        fda=OpenFDAClient(),
        buffer=timedelta(days=args.buffer_days),
    )
    # Stage first so a long network fetch is never lost to a borderline assertion,
    # then gate: a corpus that fails health is NOT promoted to the real path.
    staged = args.out.with_suffix(".jsonl.staged")
    save_corpus(chunks, staged)
    try:
        report = assert_corpus_healthy(decisions, chunks, buffer_days=args.buffer_days)
    except CorpusHealthError as exc:
        print(f"CORPUS REJECTED (not promoted): {exc}", file=sys.stderr)
        print(f"staged copy for inspection: {staged}", file=sys.stderr)
        return 1
    save_corpus(chunks, args.out)
    staged.unlink(missing_ok=True)
    print(f"corpus: {len(chunks)} chunks → {args.out}")
    print("health:", json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
