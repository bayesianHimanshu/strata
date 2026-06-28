"""Safety-Signal Surveillance — ported from VIGIL's signal agent onto the STRATA substrate.

Answers aggregate safety questions by generating a read-only SELECT against the
``vw_signal_metrics`` disproportionality view, validating it (sql_guard, deny-by-default),
executing it over FAERS, and summarising the results — grounded only in the returned rows.
FAERS reactions are already MedDRA-coded, so this needs no case-processing pipeline; it is
inherently live (as-of-now over the loaded FAERS set). Every query is append-only audited
(the persisted job record carries the exact SQL that produced every number).

PRR/ROR are SCREENING signals from spontaneous, unverified reports — not confirmed causal
risks. The caveats always say so.
"""
from __future__ import annotations

import json

from strata_platform.capabilities.base import Capability, parse_json_object
from strata_platform.substrate.contracts import (
    CapabilityRequest,
    CapabilityResult,
    GeneratedSQL,
    SignalNarration,
    SignalOutput,
    SignalRow,
)
from strata_platform.substrate.reasoner import Reasoner
from strata_platform.substrate.sql_guard import validate_readonly

_VIEW = "vw_signal_metrics"
_SCREENING_CAVEAT = ("PRR/ROR are screening signals from spontaneous, unverified FAERS "
                     "reports — not confirmed causal risks. No exposure denominator; "
                     "subject to reporting and confounding bias.")

_SQLGEN_SYSTEM = (
    "You generate ONE read-only Postgres SELECT to answer a pharmacovigilance signal "
    f"question, querying ONLY the view {_VIEW}. Columns: scope_drug, event_pt, a, b, c, d, "
    "n_total, prr, ror, signal_flag (a 2x2 disproportionality table per adverse-event "
    "MedDRA PT per suspect drug; prr=proportional reporting ratio, ror=reporting odds "
    "ratio, signal_flag = PRR>=2 AND a>=3). Rules: SELECT only; query ONLY "
    f"{_VIEW}; filter scope_drug with ILIKE when the question names a drug; order by prr "
    "DESC NULLS LAST; always include a LIMIT. Never write DML/DDL. Output strict JSON: "
    '{"sql": "<the SELECT>", "rationale": "<one line>"}.')
_NARRATE_SYSTEM = (
    "Summarise the disproportionality results for the user's question in 2-4 sentences. Be "
    "precise and cautious: PRR/ROR are screening signals from spontaneous, unverified "
    "reports, not confirmed risks. Reference only the event PTs present in the results; "
    "introduce no numbers not in the rows. List caveats (small cell counts, reporting/"
    "confounding bias, no exposure denominator). Output strict JSON: "
    '{"summary": "..", "caveats": [".."]}.')


def run_signal_sql(sql: str) -> list[dict]:  # pragma: no cover - requires live DB
    """Execute a (guarded) read-only SELECT against Postgres → list of row dicts. Module
    level so tests can monkeypatch it without a database."""
    from sqlalchemy import text

    from strata_platform.db.session import get_sessionmaker

    with get_sessionmaker()() as s:
        return [dict(r) for r in s.execute(text(sql)).mappings().all()]


def _default_sql(drug: str | None, limit: int = 25) -> str:
    where = f" WHERE scope_drug ILIKE '%{drug.replace(chr(39), '')}%'" if drug else ""
    return (f"SELECT scope_drug, event_pt, a, b, c, d, n_total, prr, ror, signal_flag "
            f"FROM {_VIEW}{where} ORDER BY prr DESC NULLS LAST LIMIT {limit}")


def _gen_sql(reasoner: Reasoner, question: str, drug: str | None) -> tuple[str, str | None]:
    """Model → GeneratedSQL → guarded safe SQL. Returns (safe_sql, reject_reason|None).
    Unsafe SQL is REJECTED and never executed; a safe default runs in its place."""
    user = f"Question: {question}" + (f"\nDrug scope: {drug}" if drug else "")
    raw = parse_json_object(reasoner.complete(user, system=_SQLGEN_SYSTEM))
    try:
        gen = GeneratedSQL.model_validate(raw)
    except Exception:  # noqa: BLE001 - malformed → fall back to the safe default
        return _default_sql(drug), "model did not return valid GeneratedSQL"
    ok, reason, safe = validate_readonly(gen.sql, {_VIEW})
    if not ok or safe is None:
        return _default_sql(drug), reason
    return safe, None


def _narrate(reasoner: Reasoner, question: str,
             results: list[SignalRow]) -> SignalNarration:
    top = [r.model_dump() for r in results[:25]]
    user = f"Question: {question}\n\nResults (top {len(top)} rows):\n{json.dumps(top, indent=2)}"
    raw = parse_json_object(reasoner.complete(user, system=_NARRATE_SYSTEM))
    try:
        return SignalNarration.model_validate(raw)
    except Exception:  # noqa: BLE001
        return SignalNarration(summary="", caveats=[])


class SafetySurveillance(Capability):
    key = "safety_surveillance"
    summary = ("Pharmacovigilance disproportionality (PRR/ROR) over FAERS via guarded "
               "text-to-SQL — screening signals, fully auditable.")

    def run(self, request: CapabilityRequest, *, reasoner: Reasoner,
            store) -> CapabilityResult:
        question = (request.params.get("question") or "").strip()
        if not question:
            raise ValueError("safety_surveillance requires params.question")
        drug = request.params.get("product_scope") or request.params.get("drug")
        min_count = int(request.params.get("min_count", 3))

        safe_sql, reject = _gen_sql(reasoner, question, drug)
        caveats: list[str] = []
        if reject:
            caveats.append(f"generated SQL rejected ({reject}); used a safe default query")

        rows = run_signal_sql(safe_sql)
        fields = set(SignalRow.model_fields)
        results = [SignalRow(**{k: v for k, v in r.items() if k in fields}) for r in rows]
        if min_count:
            results = [r for r in results if r.a >= min_count]

        narr = _narrate(reasoner, question, results)
        caveats.extend(narr.caveats)
        caveats.append(_SCREENING_CAVEAT)        # always: screening, not causal

        out = SignalOutput(
            generated_sql=safe_sql, results=results, summary=narr.summary, caveats=caveats,
            audit={"guarded_sql": safe_sql, "row_count": len(results),
                   "min_count": min_count, "llm_calls": 2, "drug_scope": drug})
        return CapabilityResult(capability=self.key, tenant_id=request.tenant_id,
                                payload=out.model_dump(mode="json"))
