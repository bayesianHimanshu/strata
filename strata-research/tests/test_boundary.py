"""Phase 2 Task 1 - corpus-composition boundary. The binding leakage guard.

Sits alongside the date-cutoff tests (test_leakage / test_store): here we assert
the SOURCE-TYPE exclusion that a date filter cannot provide - an appraisal's own
gold-bearing dossier docs, and registered same-drug sibling dossiers, are never
retrievable for that decision even when their dates clear the buffer.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from core.contracts import Decision, DocType, Span
from index.boundary import (
    CorpusBoundaryError,
    RetrievalBoundary,
    compute_sibling_map,
)
from index.leakage import LeakageError
from index.store import Chunk, InMemoryStore, structure_aware_prefixed_chunks

CUTOFF_DECISION = Decision(
    agency="NICE",
    decision_id="TA1042",
    decision_date=date(2026, 6, 1),
    indication="pembrolizumab for NSCLC",
    outcome="not_recommended",
    drug="pembrolizumab",
    appraisal_id="TA1042",
)


def _chunk(
    *, doc_date: date, doc_type: DocType | None, appraisal_id: str | None, sid="s"
) -> Chunk:
    return Chunk(
        source_id=sid,
        doc_date=doc_date,
        section_path="",
        text="t",
        raw_text="immature overall survival; comparator not UK practice",
        span=Span(start=0, end=10),
        doc_type=doc_type,
        appraisal_id=appraisal_id,
    )


def _boundary(**kw) -> RetrievalBoundary:
    return RetrievalBoundary.for_decision(
        CUTOFF_DECISION, buffer=timedelta(days=90), **kw
    )


# --- dossier-disjointness (the core invariant) ------------------------------ #


def test_own_dossier_doc_excluded_even_when_date_clears_buffer() -> None:
    b = _boundary()
    early = date(2026, 1, 1)  # clears the 90-day buffer (cutoff 2026-03-03)
    assert b.leakage.admits(early) is True  # date alone would admit it
    for dt in DocType.__members__.values():
        chunk = _chunk(doc_date=early, doc_type=dt, appraisal_id="TA1042")
        from core.contracts import DOSSIER_DOC_TYPES

        if dt in DOSSIER_DOC_TYPES:
            assert b.admits(chunk) is False, f"{dt} should be excluded"
        else:
            assert b.admits(chunk) is True, f"{dt} should be admitted"


def test_other_appraisals_dossier_doc_is_admitted() -> None:
    b = _boundary()
    chunk = _chunk(
        doc_date=date(2026, 1, 1),
        doc_type=DocType.ta_final_guidance,
        appraisal_id="TA0007",  # a different, unrelated appraisal
    )
    assert b.admits(chunk) is True


def test_assert_admits_raises_specific_dossier_reason() -> None:
    b = _boundary()
    chunk = _chunk(
        doc_date=date(2026, 1, 1),
        doc_type=DocType.ta_erg_report,
        appraisal_id="TA1042",
    )
    with pytest.raises(CorpusBoundaryError, match="dossier leakage"):
        b.assert_admits(chunk)


def test_date_violation_still_caught_under_boundary() -> None:
    b = _boundary()
    chunk = _chunk(
        doc_date=date(2026, 5, 1),  # after cutoff
        doc_type=DocType.literature,
        appraisal_id=None,
    )
    assert b.admits(chunk) is False
    with pytest.raises(LeakageError):
        b.assert_admits(chunk)


# --- same-drug sibling policy (registered research parameter) ---------------- #


def test_sibling_map_groups_by_drug() -> None:
    decisions = [
        Decision(
            agency="NICE", decision_id="TA1042", decision_date=date(2025, 1, 1),
            indication="pembro NSCLC", outcome="optimised",
            drug="Pembrolizumab", appraisal_id="TA1042",
        ),
        Decision(
            agency="NICE", decision_id="TA0900", decision_date=date(2024, 1, 1),
            indication="pembro melanoma", outcome="recommended",
            drug="pembrolizumab", appraisal_id="TA0900",
        ),
        Decision(
            agency="NICE", decision_id="TA0500", decision_date=date(2023, 1, 1),
            indication="nivo RCC", outcome="recommended",
            drug="nivolumab", appraisal_id="TA0500",
        ),
    ]
    sib = compute_sibling_map(decisions)
    assert sib["TA1042"] == frozenset({"TA0900"})  # same drug, case-insensitive
    assert sib["TA0900"] == frozenset({"TA1042"})
    assert sib["TA0500"] == frozenset()  # no sibling


def test_sibling_dossier_excluded_when_policy_on() -> None:
    b = _boundary(sibling_appraisal_ids=["TA0900"], exclude_siblings=True)
    sib_chunk = _chunk(
        doc_date=date(2025, 1, 1),
        doc_type=DocType.ta_final_guidance,
        appraisal_id="TA0900",
    )
    assert b.admits(sib_chunk) is False
    with pytest.raises(CorpusBoundaryError, match="sibling leakage"):
        b.assert_admits(sib_chunk)


def test_sibling_admitted_when_policy_off_is_logged() -> None:
    b = _boundary(sibling_appraisal_ids=["TA0900"], exclude_siblings=False)
    sib_chunk = _chunk(
        doc_date=date(2025, 1, 1),
        doc_type=DocType.ta_final_guidance,
        appraisal_id="TA0900",
    )
    assert b.admits(sib_chunk) is True
    # the policy is surfaced, never silent
    assert b.policy()["exclude_same_drug_siblings"] is False


# --- end-to-end through the store ------------------------------------------- #


def test_store_search_never_returns_own_dossier_chunk() -> None:
    store = InMemoryStore()
    dossier_text = "Background:\nThe ERG considered the OS data immature.\n"
    store.add(
        structure_aware_prefixed_chunks(
            dossier_text,
            source_id="TA1042-erg",
            doc_date=date(2026, 1, 1),
            doc_type=DocType.ta_erg_report,
            appraisal_id="TA1042",
        )
    )
    public_text = "Background:\nA registry reported immature OS for the comparator.\n"
    store.add(
        structure_aware_prefixed_chunks(
            public_text,
            source_id="ctgov-NCT1",
            doc_date=date(2025, 1, 1),
            doc_type=DocType.trial_registry,
            appraisal_id=None,
        )
    )
    b = _boundary()
    hits = store.search("immature OS comparator", boundary=b, k=10)
    assert hits, "the public registry chunk should match"
    assert all(h.chunk.appraisal_id != "TA1042" for h in hits)
    assert {h.chunk.doc_type for h in hits} == {DocType.trial_registry}
