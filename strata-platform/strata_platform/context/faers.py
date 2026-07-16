"""FAERS ingestion for the safety-signal capability.

openFDA's drug/event reactions are ALREADY MedDRA-coded (``reactionmeddrapt``), so the
signal layer computes disproportionality directly over FAERS - no intake/extraction/coding
pipeline is needed. This parses openFDA event reports into the normalised
``faers_report`` / ``faers_drug`` / ``faers_reaction`` rows the ``vw_signal_metrics`` view
consumes. The parser is pure; ingestion fetches a scoped slice (FAERS is large - full
history is the production step) and upserts idempotently.
"""
from __future__ import annotations

from strata_platform.sources.dates import normalize_date

# openFDA drugcharacterization -> role
_ROLE = {"1": "suspect", "2": "concomitant", "3": "interacting"}


def parse_faers_results(payload: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """Pure parse of a drug/event.json payload into (reports, drugs, reactions) rows."""
    reports: list[dict] = []
    drugs: list[dict] = []
    reactions: list[dict] = []
    seen_drug: set[tuple] = set()
    seen_rxn: set[tuple] = set()
    for r in payload.get("results", []) or []:
        rid = str(r.get("safetyreportid") or "").strip()
        if not rid:
            continue
        reports.append({
            "report_id": rid,
            "received_date": normalize_date(r.get("receiptdate")),
            "serious": str(r.get("serious") or "") == "1",
        })
        patient = r.get("patient", {}) or {}
        for d in patient.get("drug", []) or []:
            name = (d.get("medicinalproduct") or "").strip()
            if not name:
                openfda = d.get("openfda", {}) or {}
                gn = openfda.get("generic_name") or []
                name = (gn[0] if gn else "").strip()
            if not name:
                continue
            role = _ROLE.get(str(d.get("drugcharacterization") or ""), "concomitant")
            key = (rid, name.upper(), role)
            if key in seen_drug:
                continue
            seen_drug.add(key)
            drugs.append({"report_id": rid, "name": name.upper(), "role": role})
        for rx in patient.get("reaction", []) or []:
            pt = (rx.get("reactionmeddrapt") or "").strip()
            if not pt:
                continue
            key = (rid, pt.upper())
            if key in seen_rxn:
                continue
            seen_rxn.add(key)
            reactions.append({"report_id": rid, "pt": pt.upper()})
    return reports, drugs, reactions


def ingest_faers(session_factory, drug: str, *, client=None, limit: int = 300,
                 page: int = 100) -> dict:
    """Fetch a scoped FAERS slice for ``drug`` (as a suspect drug), parse, and upsert. The
    drug is tagged role='suspect' so the view's per-drug 2x2 is well-defined. Returns a
    small ingest summary."""
    from sqlalchemy.dialects.postgresql import insert

    from strata_platform.db.models import FaersDrugRow, FaersReactionRow, FaersReportRow
    from strata_platform.sources.openfda import OpenFDAClient

    fda = OpenFDAClient(client=client) if client else OpenFDAClient()
    search = f'patient.drug.medicinalproduct:"{drug}"'
    all_reports: dict[str, dict] = {}
    all_drugs: list[dict] = []
    all_reactions: list[dict] = []
    skip = 0
    while len(all_reports) < limit:
        payload = fda.fetch_events(search, limit=min(page, limit - len(all_reports)),
                                   skip=skip)
        reports, drugs, reactions = parse_faers_results(payload)
        if not reports:
            break
        for rep in reports:
            all_reports[rep["report_id"]] = rep
        all_drugs += drugs
        all_reactions += reactions
        skip += len(reports)
        if len(reports) < page:
            break

    with session_factory() as s:
        for rep in all_reports.values():
            s.execute(insert(FaersReportRow).values(**rep).on_conflict_do_nothing())
        for d in all_drugs:
            s.execute(insert(FaersDrugRow).values(**d).on_conflict_do_nothing())
        for rx in all_reactions:
            s.execute(insert(FaersReactionRow).values(**rx).on_conflict_do_nothing())
        s.commit()
    return {"drug": drug, "reports": len(all_reports), "drug_rows": len(all_drugs),
            "reaction_rows": len(all_reactions)}
