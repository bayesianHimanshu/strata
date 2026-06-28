"""The boundary contracts enforce invariant #1 by construction."""
from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from core.contracts import Claim, Decision, GoldItem, Span, VulnCategory, Vulnerability


def _claim(**over) -> Claim:
    base = dict(
        text="surrogate endpoint immature",
        source_id="nice:TA1000",
        span=Span(start=10, end=42),
        retrieval_score=0.71,
        doc_date=date(2025, 6, 1),
    )
    base.update(over)
    return Claim(**base)  # type: ignore[arg-type]


def test_span_rejects_reversed_offsets() -> None:
    with pytest.raises(ValidationError):
        Span(start=50, end=10)


def test_span_rejects_negative() -> None:
    with pytest.raises(ValidationError):
        Span(start=-1, end=5)


def test_claim_requires_provenance_fields() -> None:
    # doc_date is required on an emitted Claim (invariant #1).
    with pytest.raises(ValidationError):
        Claim(  # type: ignore[call-arg]
            text="x", source_id="s", span=Span(start=0, end=1), retrieval_score=0.1
        )


def test_claim_rejects_empty_text_or_source() -> None:
    with pytest.raises(ValidationError):
        _claim(text="")
    with pytest.raises(ValidationError):
        _claim(source_id="")


def test_vulnerability_confidence_bounded() -> None:
    Vulnerability(category=VulnCategory.comparator, claim=_claim(), confidence=0.0)
    Vulnerability(category=VulnCategory.comparator, claim=_claim(), confidence=1.0)
    with pytest.raises(ValidationError):
        Vulnerability(category=VulnCategory.comparator, claim=_claim(), confidence=1.5)


def test_models_are_frozen() -> None:
    v = Vulnerability(
        category=VulnCategory.missing_pro, claim=_claim(), confidence=0.5
    )
    with pytest.raises(ValidationError):
        v.confidence = 0.9  # type: ignore[misc]


def test_decision_and_golditem_minimal() -> None:
    d = Decision(
        agency="NICE",
        decision_id="TA1000",
        decision_date=date(2026, 3, 1),
        indication="NSCLC 2L",
        outcome="not_recommended",
    )
    assert d.outcome == "not_recommended"
    g = GoldItem(
        decision_id="TA1000",
        category=VulnCategory.icer_uncertainty,
        evidence_span="the committee considered the ICER highly uncertain",
    )
    assert g.category is VulnCategory.icer_uncertainty
