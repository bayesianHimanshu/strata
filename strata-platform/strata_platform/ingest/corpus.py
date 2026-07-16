"""Build the open-book retrieval corpus - per-decision, molecule-scoped, leakage-filtered.

Carries the research rebuild's three fixes: (1) each decision gathers evidence scoped to
ITS molecule(s) (sources.drug_identity), leakage-date-filtered to decision_date − buffer;
(2) trials by ``query.intr=molecule AND query.cond=indication`` (not a global sweep), label
by ``generic_name == molecule`` EXACT (no default-doc fallback - the ORSERDU bug),
literature via a relaxation chain with retmax SET so PMIDs return; (3) every chunk carries
drug / doc_type / appraisal_id / doc_date so retrieval is molecule-scoped at query time.
NICE dossiers gain drug=molecule so same-drug siblings are retrievable while a decision's
own dossier stays boundary-excluded.

Pure converters + query hygiene are unit-tested; the network orchestration is a thin
wrapper over the typed clients (which carry the Phase 0 / WAF fixes).
"""
from __future__ import annotations

import re
import sys
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from strata_platform.sources.clinicaltrials import ClinicalTrialsClient, TrialRecord
from strata_platform.sources.drug_identity import DrugIdentity, normalize_drug
from strata_platform.sources.nice_guidance import GuidanceResult
from strata_platform.sources.openfda import LabelDoc, OpenFDAClient
from strata_platform.sources.pubmed import Abstract, PubMedClient
from strata_platform.substrate.chunking import structure_aware_prefixed_chunks
from strata_platform.substrate.contracts import Chunk, Decision, DocType

_HTA_CLAUSE = (
    '(cost-effectiveness OR "health technology assessment" OR comparator OR '
    '"overall survival" OR surrogate OR endpoint OR efficacy)'
)
_DASH_MAP = {ord(c): "-" for c in "‐‑‒--―−"}



# Sibling map (same-molecule prior appraisals - registered, not silent)


def compute_sibling_map(decisions: Iterable[Decision]) -> dict[str, frozenset[str]]:
    """decision_id -> other decision_ids sharing the same primary molecule. Keyed via the
    single ``normalize_drug`` so siblings match across different combination strings (the
    raw-string bug that meant siblings never fired)."""
    decisions = list(decisions)
    by_primary: dict[str, list[str]] = {}
    for d in decisions:
        primary = normalize_drug(d.drug or "").primary
        if primary:
            by_primary.setdefault(primary, []).append(d.decision_id)
    out: dict[str, frozenset[str]] = {}
    for d in decisions:
        primary = normalize_drug(d.drug or "").primary
        peers = set(by_primary.get(primary, [])) - {d.decision_id}
        out[d.decision_id] = frozenset(peers)
    return out



# Query hygiene (NICE free-text -> parser-safe terms)


def clean_query(text: str, *, keep_hyphen: bool = True) -> str:
    """Sanitize for the CT.gov Essie / openFDA Lucene parsers (they 400 on en-dashes,
    parens, colons, slashes). Normalize unicode, map dashes, drop other specials."""
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
        return f"({mol}) AND ({clean_query(indication)}) AND {_HTA_CLAUSE}"
    return f"({mol}) AND {_HTA_CLAUSE}"



# Retrievable doc + pure source->doc converters


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


def trial_to_doc(t: TrialRecord, *, drug=None, indication=None,
                 decision_id=None) -> RetrievableDoc:
    text = "\n".join(
        s for s in (
            f"Title: {t.title}",
            f"Conditions: {', '.join(t.conditions)}" if t.conditions else "",
            f"Phase: {t.phase}" if t.phase else "",
            f"Status: {t.overall_status}" if t.overall_status else "",
            ("Primary outcomes: " + "; ".join(t.primary_outcomes))
            if t.primary_outcomes else "",
        ) if s
    )
    return RetrievableDoc(
        source="clinicaltrials.gov", source_id=t.nct_id or "NCT-unknown",
        url=f"https://clinicaltrials.gov/study/{t.nct_id}", title=t.title, text=text,
        doc_date=t.completion_date or t.start_date, doc_type=DocType.trial_registry,
        appraisal_id=None, drug=drug, indication=indication, decision_id=decision_id,
    )


def abstract_to_doc(a: Abstract, *, drug=None, indication=None,
                    decision_id=None) -> RetrievableDoc:
    return RetrievableDoc(
        source="pubmed", source_id=f"PMID:{a.pmid}",
        url=f"https://pubmed.ncbi.nlm.nih.gov/{a.pmid}/", title=a.title,
        text=f"Title: {a.title}\nAbstract: {a.abstract}", doc_date=a.pub_date,
        doc_type=DocType.literature, appraisal_id=None, drug=drug,
        indication=indication, decision_id=decision_id,
    )


def label_to_doc(label: LabelDoc, *, drug=None, indication=None,
                 decision_id=None) -> RetrievableDoc:
    name = label.brand or label.generic or drug or "drug"
    return RetrievableDoc(
        source="openfda", source_id=f"label:{drug or name}",
        url="https://api.fda.gov/drug/label.json", title=f"{name} label",
        text=label.text, doc_date=label.effective_date, doc_type=DocType.label,
        appraisal_id=None, drug=drug, indication=indication, decision_id=decision_id,
    )


def dossier_to_doc(gr: GuidanceResult, *, drug=None,
                   indication=None) -> RetrievableDoc | None:
    if gr.status != "ok" or gr.parsed is None:
        return None
    p = gr.parsed
    return RetrievableDoc(
        source="nice", source_id=f"{p.ta_id}:guidance",
        url=f"https://www.nice.org.uk/guidance/{p.ta_id.lower()}", title=p.title,
        text=p.rationale_raw, doc_date=p.published_date,
        doc_type=DocType.ta_final_guidance,  # gold-bearing -> boundary excludes its own
        appraisal_id=p.ta_id, drug=drug, indication=indication or p.title,
    )


def doc_to_chunks(doc: RetrievableDoc) -> list[Chunk]:
    """Chunk one RetrievableDoc, tagging chunks with drug/doc_type/appraisal_id/doc_date.
    Skips undated/empty docs (the leakage filter would reject undated chunks anyway)."""
    if not doc.text.strip() or doc.doc_date is None:
        return []
    return structure_aware_prefixed_chunks(
        doc.text, source_id=doc.source_id, doc_date=doc.doc_date, doc_title=doc.title,
        doc_type=doc.doc_type, appraisal_id=doc.appraisal_id, drug=doc.drug,
    )



# Fetch (network) - per molecule


def _warn(decision_id: str, source: str, exc: Exception) -> None:
    print(f"[ingest] {decision_id}: {source} fetch failed "
          f"({type(exc).__name__}: {exc}) - skipping that source", file=sys.stderr)


def fetch_literature(molecule: str, indication: str, *, pubmed: PubMedClient,
                     max_abstracts: int = 20) -> list[Abstract]:
    """esearch (retmax SET so PMIDs return) -> efetch, via a relaxation chain. The HTA/HEOR
    clause is a soft boost: molecules with no HEOR papers yet fall through to molecule
    alone (recency-capped) so clinical literature is still gathered. A final 0 warns -
    never silently swallowed."""
    mol, ind = clean_query(molecule), clean_query(indication)
    attempts: list[tuple[str, int, str | None]] = []
    if ind:
        attempts.append((pubmed_query(molecule, indication, with_indication=True),
                         max_abstracts, None))
    attempts.append((pubmed_query(molecule, indication, with_indication=False),
                     max_abstracts, None))
    if ind:
        attempts.append((f"({mol}) AND ({ind})", max_abstracts, None))
    attempts.append((f"({mol})", max(max_abstracts, 40), "pub_date"))

    for term, retmax, sort in attempts:
        res, _ = pubmed.search(term, retmax=retmax, sort=sort)
        if res.pmids:
            abstracts, _ = pubmed.fetch_abstracts(res.pmids[:retmax])
            return abstracts
    print(f"[ingest] WARNING zero-literature {molecule!r}: q=({mol})", file=sys.stderr)
    return []


def fetch_molecule_docs(molecule: str, indication: str, decision_id: str, *,
                        ct: ClinicalTrialsClient, pubmed: PubMedClient,
                        fda: OpenFDAClient, max_trials: int = 20,
                        max_abstracts: int = 20) -> list[RetrievableDoc]:
    """All public evidence for ONE molecule + indication. Each source is independent; a
    source failure is logged and skipped (a partial corpus is recoverable)."""
    docs: list[RetrievableDoc] = []
    tag = {"drug": molecule, "indication": indication, "decision_id": decision_id}
    try:
        trials, _, _ = ct.search(condition=clean_query(indication),
                                 intervention=clean_query(molecule), status=None,
                                 page_size=max_trials)
        docs += [trial_to_doc(t, **tag) for t in trials]
    except Exception as exc:  # noqa: BLE001 - one source must not kill the run
        _warn(decision_id, f"clinicaltrials[{molecule}]", exc)
    try:
        for a in fetch_literature(molecule, indication, pubmed=pubmed,
                                  max_abstracts=max_abstracts):
            docs.append(abstract_to_doc(a, **tag))
    except Exception as exc:  # noqa: BLE001
        _warn(decision_id, f"pubmed[{molecule}]", exc)
    try:
        labels, _ = fda.fetch_label_docs(
            f'openfda.generic_name:"{primary_generic(molecule)}"'
        )
        docs += [label_to_doc(label, **tag) for label in labels]
    except Exception as exc:  # noqa: BLE001
        _warn(decision_id, f"openfda[{molecule}]", exc)
    return docs


def _drug_from_title(title: str) -> DrugIdentity:
    head = re.split(r"\bfor\b", title, maxsplit=1, flags=re.IGNORECASE)[0]
    return normalize_drug(head)


def build_corpus(decisions: Iterable[Decision], *, ct: ClinicalTrialsClient,
                 pubmed: PubMedClient, fda: OpenFDAClient,
                 dossiers: Iterable[GuidanceResult] = (),
                 buffer_days: int = 90) -> list[Chunk]:
    """NICE dossiers (molecule-tagged) + per-decision per-molecule public evidence, each
    date-filtered to the decision's leakage cutoff. Returns platform Chunks (unembedded);
    callers embed + persist after ``assert_corpus_healthy`` passes."""
    decisions = list(decisions)
    by_ta = {d.decision_id: d for d in decisions}
    chunks: list[Chunk] = []
    seen_source_ids: set[str] = set()

    for gr in dossiers:
        owner = by_ta.get(gr.ta_id)
        di = (normalize_drug(owner.drug or "") if owner
              else _drug_from_title(gr.parsed.title if gr.parsed else ""))
        doc = dossier_to_doc(gr, drug=di.primary or None,
                             indication=owner.indication if owner else None)
        if doc is not None and doc.source_id not in seen_source_ids:
            seen_source_ids.add(doc.source_id)
            chunks += doc_to_chunks(doc)

    for d in decisions:
        di = normalize_drug(d.drug or "")
        cutoff = d.decision_date - timedelta(days=buffer_days)
        for molecule in sorted(di.molecules):
            for doc in fetch_molecule_docs(molecule, d.indication, d.decision_id,
                                           ct=ct, pubmed=pubmed, fda=fda):
                if doc.doc_date is None or doc.doc_date >= cutoff:
                    continue  # not strictly before this decision's cutoff
                if doc.source_id in seen_source_ids:
                    continue
                seen_source_ids.add(doc.source_id)
                chunks += doc_to_chunks(doc)
    return chunks
