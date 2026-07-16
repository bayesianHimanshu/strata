"""Platform tests - no Azure, no DB, no network. The boundary tests are the
trust-critical ones (a leaky query must be unrepresentable)."""
from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from strata_platform.api.main import app
from strata_platform.capabilities.registry import get_capability, list_capabilities
from strata_platform.eval.harness import cohen_kappa, per_category, rubric_hash
from strata_platform.substrate.boundary import RetrievalBoundary
from strata_platform.substrate.contracts import (
    CapabilityRequest,
    Chunk,
    Decision,
    DocType,
    VulnCategory,
)
from strata_platform.substrate.reasoner import EchoReasoner
from strata_platform.substrate.store import InMemoryStore


def _boundary():
    return RetrievalBoundary(decision_id="TA1133", decision_date=date(2026, 2, 18),
                             molecules=frozenset({"belantamab mafodotin"}))


def test_boundary_excludes_own_dossier():
    b = _boundary()
    own = Chunk(text="x", doc_type=DocType.ta_final_guidance, appraisal_id="TA1133",
                drug="belantamab mafodotin", doc_date=date(2025, 1, 1), source_id="s")
    assert b.admits(own) is False


def test_boundary_excludes_post_cutoff():
    b = _boundary()
    late = Chunk(text="x", doc_type=DocType.literature, drug="belantamab mafodotin",
                 doc_date=date(2026, 2, 1), source_id="s")   # within buffer
    assert b.admits(late) is False


def test_boundary_admits_eligible_evidence():
    b = _boundary()
    ok = Chunk(text="immature OS surrogate", doc_type=DocType.literature,
               drug="belantamab mafodotin", doc_date=date(2024, 1, 1), source_id="s")
    assert b.admits(ok) is True


def test_boundary_excludes_wrong_drug():
    b = _boundary()
    wrong = Chunk(text="x", doc_type=DocType.label, drug="elacestrant",
                  doc_date=date(2024, 1, 1), source_id="s")
    assert b.admits(wrong) is False


def test_registry_has_four_capabilities():
    assert {c["key"] for c in list_capabilities()} == {
        "hta_archaeology", "endpoint_landscape", "evidence_synthesis", "safety_surveillance"}


def test_closed_book_dispatch_runs():
    cap = get_capability("hta_archaeology")
    req = CapabilityRequest(capability="hta_archaeology",
                            decision=Decision(decision_id="TA1", decision_date=date(2026, 3, 1),
                                              drug="osimertinib", indication="nsclc"),
                            params={"mode": "closed_book"})
    res = cap.run(req, reasoner=EchoReasoner(), store=InMemoryStore())
    assert res.capability == "hta_archaeology"   # echo returns [] -> no vulns, runs clean


def test_eval_metrics_and_rubric():
    gold = {"d": {VulnCategory.icer_uncertainty}}
    pred = {"d": {VulnCategory.icer_uncertainty, VulnCategory.comparator}}
    m = per_category(["d"], gold, pred)
    assert m["micro_recall"] == 1.0
    assert abs(cohen_kappa([1, 1, 0, 0], [1, 0, 0, 0]) - 0.5) < 1e-9
    assert len(rubric_hash()) == 64


def test_api_health_and_capabilities():
    c = TestClient(app)
    assert c.get("/health").status_code == 200
    assert len(c.get("/capabilities").json()["capabilities"]) == 4
