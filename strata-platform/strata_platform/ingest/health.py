"""Fail-loud corpus health gate (ported from research introspect_retrieval +
build_retrieval_corpus.assert_corpus_healthy).

This is the guard that stopped two wasted runs. It RAISES unless the corpus is
decision-specific (not a blob), literature actually landed, and retrieval routes to the
right molecule - so a broken corpus can never silently produce a fake precision/recall
delta. Operates on platform Chunks + the RetrievalBoundary (same gate as retrieval).
"""
from __future__ import annotations

from collections import Counter
from datetime import date

from strata_platform.ingest.corpus import compute_sibling_map
from strata_platform.sources.drug_identity import normalize_drug
from strata_platform.substrate.boundary import RetrievalBoundary
from strata_platform.substrate.contracts import Chunk, Decision, DocType
from strata_platform.substrate.store import InMemoryStore


class CorpusHealthError(RuntimeError):
    """Raised by the post-build gate when the corpus is a blob / mistargeted / starved."""


def composition(chunks: list[Chunk]) -> dict:
    by_type = Counter(c.doc_type.value for c in chunks)
    dates = [c.doc_date for c in chunks if c.doc_date]
    appraisals = {c.appraisal_id for c in chunks if c.appraisal_id}
    drugs = {c.drug for c in chunks if c.drug}
    lit = by_type.get(DocType.literature.value, 0)
    return {
        "total_chunks": len(chunks),
        "by_doc_type": dict(by_type),
        "literature_chunks": lit,
        "literature_missing": lit == 0,
        "distinct_appraisal_ids": len(appraisals),
        "distinct_drugs": len(drugs),
        "date_range": [min(dates).isoformat(), max(dates).isoformat()] if dates else None,
    }


def _boundary_for(d: Decision, sib: dict, buffer_days: int) -> RetrievalBoundary:
    return RetrievalBoundary(
        decision_id=d.decision_id,
        decision_date=d.decision_date,
        molecules=normalize_drug(d.drug or "").molecules,
        buffer_days=buffer_days,
        exclude_siblings=True,
        sibling_ids=sib.get(d.decision_id, frozenset()),
    )


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
    comp = composition(chunks)

    if comp["distinct_drugs"] <= 1 or comp["distinct_drugs"] < min_distinct_ratio * n:
        raise CorpusHealthError(
            f"blob: distinct_drugs={comp['distinct_drugs']} not ≈ n_decisions={n} "
            f"(min {min_distinct_ratio:.0%}) - chunks are not molecule-specific"
        )
    if comp["literature_chunks"] == 0:
        raise CorpusHealthError(
            "literature arm did not land (0 literature chunks) - check the PubMed query"
        )

    store = InMemoryStore()
    store.add(chunks)
    sib = compute_sibling_map(decisions)

    pool_sigs: set[frozenset] = set()
    lit_present = 0
    matched = total = 0
    for d in decisions:
        di = normalize_drug(d.drug or "")
        boundary = _boundary_for(d, sib, buffer_days)
        eligible = [c for c in store.chunks if boundary.admits(c)]
        pool_sigs.add(frozenset(c.chunk_id for c in eligible))
        if any(c.doc_type == DocType.literature for c in eligible):
            lit_present += 1
        for hit in store.search(probe_query, boundary, k=k):
            total += 1
            if hit.drug in di.molecules:
                matched += 1

    if n > 1 and len(pool_sigs) <= 1:
        raise CorpusHealthError(
            "blob: every decision has an identical eligible pool - retrieval is not "
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
        "date_range": comp["date_range"],
        "as_of": date.today().isoformat(),
    }
