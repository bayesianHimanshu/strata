"""Arm A synthesizer: open/closed-book, provenance, boundary, rubric gating."""
from __future__ import annotations

from datetime import date

import pytest

from agents.synthesizer import (
    EvidenceGapSynthesizer,
    KeywordReasoner,
    attributable_signal,
)
from core.contracts import (
    Claim,
    Decision,
    DocType,
    Prediction,
    Span,
    VulnCategory,
    Vulnerability,
)
from eval import rubric
from index.store import InMemoryStore, structure_aware_prefixed_chunks

C = VulnCategory

PUBLIC_DOC = (
    "Limitations:\n"
    "The overall survival data from the registry were immature at the data cut. "
    "The comparator arm did not reflect NHS clinical practice. "
    "No health-related quality of life data were collected.\n"
)
# Same-appraisal dossier doc: date clears the buffer, but it is gold-bearing.
DOSSIER_DOC = (
    "ERG critique:\n"
    "The ICER was highly uncertain and the single-arm trial design carried a high "
    "risk of bias.\n"
)

TARGET = Decision(
    agency="NICE",
    decision_id="TA1042",
    decision_date=date(2026, 6, 1),
    indication="pembrolizumab for NSCLC",
    outcome="not_recommended",
    drug="pembrolizumab",
    appraisal_id="TA1042",
)


def _store() -> InMemoryStore:
    store = InMemoryStore()
    store.add(
        structure_aware_prefixed_chunks(
            PUBLIC_DOC,
            source_id="ctgov-NCT1",
            doc_date=date(2025, 1, 1),
            doc_type=DocType.trial_registry,
            appraisal_id=None,
        )
    )
    store.add(
        structure_aware_prefixed_chunks(
            DOSSIER_DOC,
            source_id="TA1042-erg",
            doc_date=date(2025, 6, 1),  # clears the 90-day buffer
            doc_type=DocType.ta_erg_report,
            appraisal_id="TA1042",
        )
    )
    return store


def test_open_book_emits_grounded_vulnerabilities() -> None:
    synth = EvidenceGapSynthesizer(store=_store())
    res = synth.synthesize(TARGET, mode="open")
    assert res.mode == "open"
    cats = {v.category for v in res.vulnerabilities}
    assert C.surrogate_endpoint_immaturity in cats
    assert C.comparator in cats
    assert C.missing_pro in cats
    # invariant #1: every emitted vulnerability carries full provenance
    for v in res.vulnerabilities:
        assert isinstance(v, Vulnerability)
        assert v.claim.source_id and v.claim.doc_date and v.claim.span.end >= 0
        assert 0.0 <= v.confidence <= 1.0
    assert res.boundary_policy is not None


def test_open_book_never_retrieves_own_dossier_doc() -> None:
    # The dossier doc is the ONLY source of ICER / trial-design language. The boundary
    # must keep it out, so those categories must NOT appear and its source never cited.
    synth = EvidenceGapSynthesizer(store=_store())
    res = synth.synthesize(TARGET, mode="open")
    cats = {v.category for v in res.vulnerabilities}
    assert C.icer_uncertainty not in cats
    assert C.trial_design_bias not in cats
    assert all(v.claim.source_id != "TA1042-erg" for v in res.vulnerabilities)


def test_closed_book_is_ungrounded_and_ignores_store() -> None:
    res_with = EvidenceGapSynthesizer(store=_store()).synthesize(TARGET, mode="closed")
    res_empty = EvidenceGapSynthesizer(store=InMemoryStore()).synthesize(
        TARGET, mode="closed"
    )
    # closed-book emits Predictions only — no grounded Vulnerabilities at all
    assert res_with.vulnerabilities == ()
    assert all(isinstance(p, Prediction) for p in res_with.predictions)
    # and it is independent of store contents (it never retrieves)
    assert res_with.predicted_pairs() == res_empty.predicted_pairs()
    assert res_with.n_claims_retrieved == 0


def test_attributable_signal_is_open_minus_closed() -> None:
    store = _store()
    synth = EvidenceGapSynthesizer(store=store)
    open_res = synth.synthesize(TARGET, mode="open")
    closed_res = synth.synthesize(TARGET, mode="closed")
    attributable = attributable_signal(open_res, closed_res)
    assert attributable == open_res.predicted_pairs() - closed_res.predicted_pairs()
    assert attributable  # open-book found grounded gaps the prior did not


def test_attributable_subtraction_removes_prior_hits() -> None:
    # Hand-built results to show subtraction explicitly.
    claim = Claim(
        text="comparator did not reflect practice",
        source_id="s",
        span=Span(start=0, end=5),
        retrieval_score=0.4,
        doc_date=date(2025, 1, 1),
    )
    from agents.synthesizer import SynthesisResult

    open_res = SynthesisResult(
        "TA1",
        "open",
        vulnerabilities=(
            Vulnerability(category=C.comparator, claim=claim, confidence=0.5),
            Vulnerability(category=C.missing_pro, claim=claim, confidence=0.5),
        ),
    )
    closed_res = SynthesisResult(
        "TA1",
        "closed",
        predictions=(Prediction(category=C.comparator, confidence=0.3),),
    )
    assert attributable_signal(open_res, closed_res) == {("TA1", C.missing_pro)}


def test_run_blocked_when_rubric_not_committed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Invariant #6: no synthesizer run while the rubric is unregistered/drifted.
    monkeypatch.setattr(rubric, "committed_hash", lambda: None)
    synth = EvidenceGapSynthesizer(store=_store())
    with pytest.raises(RuntimeError, match="not pre-registered"):
        synth.synthesize(TARGET, mode="open")


def test_keyword_reasoner_confidence_bounded() -> None:
    r = KeywordReasoner()
    claim = Claim(
        text="the overall survival data were immature",
        source_id="s",
        span=Span(start=0, end=5),
        retrieval_score=9.0,
        doc_date=date(2025, 1, 1),
    )
    vulns = r.open_book(TARGET, [claim])
    assert vulns and all(0.0 <= v.confidence <= 1.0 for v in vulns)
