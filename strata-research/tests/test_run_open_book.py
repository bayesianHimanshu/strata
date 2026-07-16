"""Task 2: evidence-bearing sub-slice delta, scored without extra model calls."""
from __future__ import annotations

from datetime import date

from eval.closed_book_probe import ProbeDecision
from experiments.run_open_book import evidence_bearing_ids, run_open_book


def _chunk(sid, drug, doc_date="2025-01-01", appraisal_id=None):
    return {
        "source_id": sid, "drug": drug, "doc_date": doc_date,
        "appraisal_id": appraisal_id, "doc_type": "literature", "span": [0, 1],
    }


# molecule-tagged literature exists for osimertinib, NOT for vorasidenib
CHUNKS = [_chunk("PMID:1", "osimertinib"), _chunk("PMID:2", "osimertinib")]

POST = [
    ProbeDecision(decision_id="TA_ev", decision_date=date(2026, 6, 1),
                  agency="NICE", drug="Osimertinib", indication="NSCLC"),
    ProbeDecision(decision_id="TA_noev", decision_date=date(2026, 6, 1),
                  agency="NICE", drug="Vorasidenib", indication="glioma"),
]


def test_evidence_bearing_ids_selects_only_decisions_with_own_molecule_evidence() -> None:
    dicts = [{"decision_id": d.decision_id, "decision_date": d.decision_date.isoformat(),
              "drug": d.drug} for d in POST]
    ids = evidence_bearing_ids(dicts, CHUNKS, buffer_days=90, min_eligible=1)
    assert ids == ["TA_ev"]  # vorasidenib has no eligible own-molecule chunk


class _CountingSynth:
    def __init__(self):
        self.calls = 0

    def predict_open_book(self, decision, *, buffer_days):
        self.calls += 1
        return {"comparator"}  # same prediction for all


def test_both_deltas_share_one_prediction_pass() -> None:
    synth = _CountingSynth()
    gold = {"TA_ev": {"comparator"}, "TA_noev": {"comparator"}}
    closed = {"TA_ev": {"comparator", "missing_pro"}, "TA_noev": {"comparator"}}
    taxonomy = ["comparator", "missing_pro"]

    report = run_open_book(
        POST, gold, synth, closed, taxonomy, date(2025, 12, 1),
        buffer_days=90, chunks=CHUNKS, min_eligible=1)

    # predict_open_book called exactly once per post decision - the EB slice is FREE
    assert synth.calls == 2
    assert report["micro"]["n_decisions"] == 2
    assert report["micro_evidence_bearing"]["n_decisions"] == 1
    assert report["evidence_bearing_ids"] == ["TA_ev"]
    assert "headline_evidence_bearing" in report


def test_no_chunks_means_no_evidence_bearing_block() -> None:
    synth = _CountingSynth()
    report = run_open_book(
        POST, {"TA_ev": set(), "TA_noev": set()}, synth, {}, ["comparator"],
        date(2025, 12, 1), buffer_days=90, chunks=None)
    assert "micro_evidence_bearing" not in report
    assert "micro" in report
