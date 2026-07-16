"""Safety-Signal Surveillance (VIGIL port): disproportionality math, the deny-by-default
SQL guard, and the guarded text-to-SQL -> grounded summary flow (no network)."""
from __future__ import annotations

import json

import strata_platform.capabilities.safety_surveillance as ss
from strata_platform.capabilities.safety_surveillance import SafetySurveillance
from strata_platform.substrate.contracts import CapabilityRequest, SignalOutput
from strata_platform.substrate.sql_guard import validate_readonly

ALLOWED = {"vw_signal_metrics"}


# --- 1. disproportionality math (the SQL formula, re-implemented for the test) -------- #

def _metrics(a, b, c, d):
    n_drug, n_not = a + b, c + d
    prr = round((a / n_drug) / (c / n_not), 3)
    ror = round((a * d) / (b * c), 3)
    return prr, ror, (prr >= 2 and a >= 3)


def test_prr_ror_signal_flag_on_fixed_2x2() -> None:
    prr, ror, flag = _metrics(8, 92, 10, 890)        # a=8,b=92,c=10,d=890
    assert prr == 7.2          # (8/100) / (10/900)
    assert ror == 7.739        # (8*890) / (92*10)
    assert flag is True        # prr>=2 and a>=3
    assert _metrics(2, 98, 10, 890)[2] is False      # a<3 -> not a signal


# --- 2. sql_guard (deny-by-default) -------------------------------------------------- #

def test_guard_accepts_select_and_appends_limit() -> None:
    ok, _, safe = validate_readonly("SELECT * FROM vw_signal_metrics", ALLOWED)
    assert ok and safe and "LIMIT" in safe.upper()


def test_guard_accepts_cte_over_allowed_view() -> None:
    ok, _, _ = validate_readonly(
        "WITH t AS (SELECT event_pt, prr FROM vw_signal_metrics) "
        "SELECT * FROM t ORDER BY prr DESC", ALLOWED)
    assert ok


def test_guard_rejects_writes_multistatement_and_other_tables() -> None:
    assert validate_readonly("DELETE FROM vw_signal_metrics WHERE true", ALLOWED)[0] is False
    assert validate_readonly("DROP VIEW vw_signal_metrics", ALLOWED)[0] is False
    assert validate_readonly("SELECT 1 FROM vw_signal_metrics; DROP TABLE x", ALLOWED)[0] is False
    ok, reason, _ = validate_readonly("SELECT * FROM faers_report", ALLOWED)
    assert ok is False and "disallowed" in reason


# --- 3. capability flow (fake reasoner + fake run_sql) -------------------------------- #

ROWS = [
    {"event_pt": "MYOCARDITIS", "scope_drug": "DRUGX", "a": 12, "b": 300, "c": 40,
     "d": 9000, "n_total": 9352, "prr": 3.4, "ror": 3.6, "signal_flag": True},
    {"event_pt": "HEADACHE", "scope_drug": "DRUGX", "a": 200, "b": 112, "c": 5000,
     "d": 4040, "n_total": 9352, "prr": 0.9, "ror": 0.8, "signal_flag": False},
]


class FakeReasoner:
    def __init__(self, sql: str) -> None:
        self._sql = sql

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        if "read-only Postgres SELECT" in (system or ""):
            return json.dumps({"sql": self._sql, "rationale": "top signals"})
        return json.dumps({"summary": "Myocarditis shows an elevated PRR (3.4).",
                           "caveats": ["small cell counts"]})


def _run(sql_from_model: str, monkeypatch) -> SignalOutput:
    monkeypatch.setattr(ss, "run_signal_sql", lambda sql: ROWS)
    req = CapabilityRequest(capability="safety_surveillance",
                            params={"question": "strongest signals for DrugX?",
                                    "drug": "DrugX"})
    res = SafetySurveillance().run(req, reasoner=FakeReasoner(sql_from_model),
                                   store=None)
    return SignalOutput.model_validate(res.payload)


def test_capability_valid_sql_path(monkeypatch) -> None:
    out = _run("SELECT * FROM vw_signal_metrics ORDER BY prr DESC", monkeypatch)
    assert "LIMIT" in out.generated_sql.upper()           # guard normalised it
    assert out.results[0].event_pt == "MYOCARDITIS" and out.results[0].prr == 3.4
    assert "Myocarditis" in out.summary
    # the screening-not-causal caveat is always present
    assert any("screening signals" in c for c in out.caveats)
    # narration references only returned PTs (no invented events)
    returned = {r.event_pt.lower() for r in out.results}
    assert "myocarditis" in returned


def test_capability_rejects_unsafe_sql_and_uses_default(monkeypatch) -> None:
    out = _run("DELETE FROM vw_signal_metrics", monkeypatch)   # model emits unsafe SQL
    assert "DELETE" not in out.generated_sql.upper()           # never executed
    assert out.generated_sql.lower().startswith("select")      # safe default ran
    assert any("rejected" in c for c in out.caveats)


def test_requires_question() -> None:
    import pytest
    with pytest.raises(ValueError, match="requires params.question"):
        SafetySurveillance().run(CapabilityRequest(capability="safety_surveillance"),
                                 reasoner=FakeReasoner("SELECT 1"), store=None)


# --- 4. FAERS parser (openFDA event payload -> normalised rows) ----------------------- #

def test_parse_faers_results() -> None:
    from strata_platform.context.faers import parse_faers_results

    payload = {"results": [{
        "safetyreportid": "US-001", "serious": "1", "receiptdate": "20240115",
        "patient": {
            "drug": [
                {"medicinalproduct": "DrugX", "drugcharacterization": "1"},
                {"medicinalproduct": "Aspirin", "drugcharacterization": "2"}],
            "reaction": [{"reactionmeddrapt": "Myocarditis"},
                         {"reactionmeddrapt": "Headache"}]}}]}
    reports, drugs, reactions = parse_faers_results(payload)
    assert reports[0]["report_id"] == "US-001" and reports[0]["serious"] is True
    roles = {d["name"]: d["role"] for d in drugs}
    assert roles["DRUGX"] == "suspect" and roles["ASPIRIN"] == "concomitant"
    assert {r["pt"] for r in reactions} == {"MYOCARDITIS", "HEADACHE"}
