"""HTA-archaeology evaluation: run the capability closed-book (parametric) and open-book
(retrieval-grounded) over a decision set and score both against the SME gold.

This is the platform's proof: under the leakage boundary, retrieval over public evidence
converts an over-confident parametric prior into a disciplined, higher-precision predictor.
The rubric is asserted committed (invariant #6) before any scoring; only retrieval differs
between the two arms (invariant #8 - same model, same prompt skeleton).
"""
from __future__ import annotations

from strata_platform.capabilities.hta_archaeology import HTAArchaeology
from strata_platform.eval.harness import assert_rubric_committed, open_vs_closed
from strata_platform.ingest.corpus import compute_sibling_map
from strata_platform.substrate.contracts import CapabilityRequest, Decision


def _predict(cap: HTAArchaeology, d: Decision, mode: str, *, reasoner, store,
             sibling_ids=()) -> set[str]:
    req = CapabilityRequest(capability="hta_archaeology", decision=d,
                            params={"mode": mode, "sibling_ids": list(sibling_ids)})
    res = cap.run(req, reasoner=reasoner, store=store)
    return {v.category.value for v in res.vulnerabilities}


def run_hta_eval(decisions: list[Decision], gold: dict[str, set[str]], *, reasoner,
                 store) -> dict:
    """Closed vs open book over ``decisions``, scored against ``gold``. Returns the
    open_vs_closed report (per-category precision/recall + signed deltas)."""
    assert_rubric_committed()
    cap = HTAArchaeology()
    sib = compute_sibling_map(decisions)

    decision_ids = [d.decision_id for d in decisions]
    closed_pred: dict[str, set[str]] = {}
    open_pred: dict[str, set[str]] = {}
    for d in decisions:
        closed_pred[d.decision_id] = _predict(cap, d, "closed_book",
                                              reasoner=reasoner, store=store)
        open_pred[d.decision_id] = _predict(
            cap, d, "open_book", reasoner=reasoner, store=store,
            sibling_ids=sib.get(d.decision_id, frozenset()))

    report = open_vs_closed(decision_ids, gold, closed_pred, open_pred)
    report["closed_predictions"] = {k: sorted(v) for k, v in closed_pred.items()}
    report["open_predictions"] = {k: sorted(v) for k, v in open_pred.items()}
    return report
