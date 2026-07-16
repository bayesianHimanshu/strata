"""Chunking shape + the store's leakage gate (invariant #2 at retrieval time)."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from index.leakage import LeakageFilter
from index.store import (
    InMemoryStore,
    structure_aware_prefixed_chunks,
)

DOC = (
    "Background:\n"
    "The appraisal concerns a PD-L1 inhibitor in second-line NSCLC.\n"
    "Comparator:\n"
    "The committee noted the chosen comparator did not reflect UK practice.\n"
    "ICER:\n"
    "The incremental cost-effectiveness ratio was considered highly uncertain.\n"
)


def test_chunks_are_breadcrumb_prefixed_and_section_aware() -> None:
    chunks = structure_aware_prefixed_chunks(
        DOC, source_id="nice:TA1000", doc_title="Pembro NSCLC", doc_date=date(2025, 1, 1)
    )
    sections = {c.section_path for c in chunks}
    assert {"Background", "Comparator", "ICER"} <= sections
    comp = next(c for c in chunks if c.section_path == "Comparator")
    assert comp.text.startswith("[nice:TA1000 > Pembro NSCLC > Comparator]")
    # span indexes the raw document, not the prefixed text
    assert DOC[comp.span.start : comp.span.end] == comp.raw_text


def test_search_excludes_post_cutoff_documents() -> None:
    store = InMemoryStore()
    store.add(
        structure_aware_prefixed_chunks(
            DOC, source_id="leaky", doc_date=date(2026, 5, 1)
        )
    )
    store.add(
        structure_aware_prefixed_chunks(
            DOC, source_id="clean", doc_date=date(2025, 1, 1)
        )
    )
    leakage = LeakageFilter(decision_date=date(2026, 6, 1), buffer=timedelta(days=90))
    hits = store.search("comparator uncertain ICER", boundary=leakage, k=10)
    assert hits, "expected lexical matches from the clean document"
    assert {h.chunk.source_id for h in hits} == {"clean"}


def test_undated_chunks_never_retrieved() -> None:
    store = InMemoryStore()
    store.add(structure_aware_prefixed_chunks(DOC, source_id="undated", doc_date=None))
    leakage = LeakageFilter(decision_date=date(2026, 6, 1))
    assert store.search("comparator", boundary=leakage, k=10) == []


def test_hit_to_claim_carries_provenance() -> None:
    store = InMemoryStore()
    store.add(
        structure_aware_prefixed_chunks(
            DOC, source_id="clean", doc_date=date(2025, 1, 1)
        )
    )
    leakage = LeakageFilter(decision_date=date(2026, 6, 1), buffer=timedelta(days=90))
    hit = store.search("comparator", boundary=leakage, k=1)[0]
    claim = hit.to_claim()
    assert claim.source_id == "clean"
    assert claim.doc_date == date(2025, 1, 1)
    assert claim.retrieval_score == hit.score


def test_undated_hit_cannot_become_claim() -> None:
    from core.contracts import Span
    from index.store import Chunk, Hit

    hit = Hit(
        Chunk(
            source_id="x",
            doc_date=None,
            section_path="",
            text="t",
            raw_text="t",
            span=Span(start=0, end=1),
        ),
        score=1.0,
    )
    with pytest.raises(ValueError, match="undated"):
        hit.to_claim()


def test_qdrant_backend_fails_loud_until_wired() -> None:
    from index.store import QdrantStore

    with pytest.raises(NotImplementedError):
        QdrantStore()
