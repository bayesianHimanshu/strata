"""The grounding gate + the headline finding's shape: under retrieval grounding, an
over-confident parametric prior becomes a disciplined, higher-precision predictor."""
from __future__ import annotations

from datetime import date

from strata_platform.capabilities.hta_archaeology import HTAArchaeology
from strata_platform.eval.hta_eval import run_hta_eval
from strata_platform.eval.harness import ground_categories
from strata_platform.substrate.contracts import (
    CapabilityRequest,
    Chunk,
    Decision,
    DocType,
    VulnCategory,
)
from strata_platform.substrate.store import InMemoryStore


class _StubReasoner:
    """A prior-saturated closed book: predicts a broad fixed set regardless of prompt, so
    the ONLY thing that differs between arms is retrieval grounding."""

    PREDICT = '["icer_uncertainty","comparator","missing_pro","surrogate_endpoint_immaturity"]'

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        return self.PREDICT


def _decision() -> Decision:
    return Decision(decision_id="TA1156", decision_date=date(2026, 5, 21),
                    drug="osimertinib", indication="nsclc", outcome="optimised")


def _store() -> InMemoryStore:
    s = InMemoryStore()
    s.add([
        Chunk(text="The ICER was highly uncertain above the range.",
              doc_type=DocType.literature, drug="osimertinib",
              doc_date=date(2024, 1, 1), source_id="PMID:1"),
        Chunk(text="Overall survival data were immature; reliance on a surrogate endpoint.",
              doc_type=DocType.literature, drug="osimertinib",
              doc_date=date(2024, 2, 1), source_id="PMID:2"),
    ])
    return s


def test_ground_categories_keeps_only_supported() -> None:
    chunks = _store().chunks
    predicted = {VulnCategory.icer_uncertainty, VulnCategory.comparator,
                 VulnCategory.surrogate_endpoint_immaturity, VulnCategory.missing_pro}
    grounded = ground_categories(predicted, chunks)
    assert set(grounded) == {VulnCategory.icer_uncertainty,
                             VulnCategory.surrogate_endpoint_immaturity}
    # provenance: each kept category maps to a real supporting chunk
    assert grounded[VulnCategory.icer_uncertainty].source_id == "PMID:1"


def test_open_book_drops_ungrounded_and_carries_provenance() -> None:
    cap = HTAArchaeology()
    req = CapabilityRequest(capability="hta_archaeology", decision=_decision(),
                            params={"mode": "open_book"})
    res = cap.run(req, reasoner=_StubReasoner(), store=_store())
    cats = {v.category for v in res.vulnerabilities}
    assert cats == {VulnCategory.icer_uncertainty,
                    VulnCategory.surrogate_endpoint_immaturity}
    assert "comparator" in res.payload["dropped_ungrounded"]
    assert all(v.grounded and v.provenance and v.provenance.chunk_ids
               for v in res.vulnerabilities)


def test_finding_shape_precision_up_under_grounding() -> None:
    d = _decision()
    gold = {d.decision_id: {"icer_uncertainty", "surrogate_endpoint_immaturity"}}
    report = run_hta_eval([d], gold, reasoner=_StubReasoner(), store=_store())
    cp = report["closed_book"]["micro_precision"]
    op = report["open_book"]["micro_precision"]
    assert op > cp                       # the finding: grounding raises precision
    assert op == 1.0 and cp == 0.5       # closed emits 4 (2 wrong); open grounds to 2
    assert report["open_book"]["micro_recall"] == report["closed_book"]["micro_recall"]
    assert report["delta"]["micro_precision"] == 0.5
    assert len(report["rubric_hash"]) == 64
