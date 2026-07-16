"""ClinicalTrials.gov API v2 client.

No auth, cursor pagination, ``countTotal`` supported. ``parse_study`` is a pure function
over one v2 study object and is unit-tested without the network.

CT.gov sits behind Akamai Bot Manager, which fingerprints the TLS handshake (JA3/JA4) and
403s a plain httpx client even with a browser User-Agent and a US IP. We therefore default
to a ``curl_cffi`` session impersonating Chrome - the Phase-0-hardened fix. Any client
exposing ``.get(url, params=...)`` (incl. httpx) can be injected for tests.
"""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel

from strata_platform.sources.dates import normalize_date
from strata_platform.sources.endpoints import CTGOV_BASE
from strata_platform.substrate.contracts import DocType, SourceRecord
from strata_platform.substrate.provenance import snapshot


def _default_session():
    """A Chrome-TLS-impersonating session that clears Akamai's handshake filter."""
    from curl_cffi import requests as cffi

    return cffi.Session(impersonate="chrome", timeout=30)


class TrialRecord(BaseModel):
    model_config = {"frozen": True}

    nct_id: str
    title: str
    conditions: list[str] = []
    phase: str | None = None
    overall_status: str | None = None
    start_date: date | None = None
    completion_date: date | None = None
    primary_outcomes: list[str] = []


def parse_study(study: dict) -> TrialRecord:
    """Extract a typed record from a v2 ``studies[i]`` object. Tolerant of missing
    fields - CT.gov omits sections freely."""
    ps = study.get("protocolSection", {})
    ident = ps.get("identificationModule", {})
    status = ps.get("statusModule", {})
    cond = ps.get("conditionsModule", {})
    design = ps.get("designModule", {})
    outcomes = ps.get("outcomesModule", {})

    phases = design.get("phases") or []
    return TrialRecord(
        nct_id=ident.get("nctId", ""),
        title=ident.get("briefTitle") or ident.get("officialTitle") or "",
        conditions=list(cond.get("conditions") or []),
        phase=", ".join(phases) if phases else None,
        overall_status=status.get("overallStatus"),
        start_date=normalize_date((status.get("startDateStruct") or {}).get("date")),
        completion_date=normalize_date(
            (status.get("completionDateStruct") or {}).get("date")
        ),
        primary_outcomes=[
            o.get("measure", "") for o in (outcomes.get("primaryOutcomes") or [])
        ],
    )



# Structured fetch - preserves outcomes + arms for the endpoint/comparator landscape.
# Endpoints and comparators are STRUCTURED fields in the v2 registration; we keep them
# structured (don't flatten to text) so counts are deterministic and auditable.


_COMPARATOR_ARM_TYPES = {"ACTIVE_COMPARATOR", "PLACEBO_COMPARATOR", "NO_INTERVENTION",
                         "SHAM_COMPARATOR"}


class Outcome(BaseModel):
    model_config = {"frozen": True}
    measure: str                       # raw outcome text from the registry
    kind: str                          # "primary" | "secondary"


class Arm(BaseModel):
    model_config = {"frozen": True}
    label: str
    intervention_names: list[str] = []
    is_comparator: bool = False        # control / active-comparator / placebo arms


class StructuredTrial(BaseModel):
    nct_id: str
    title: str
    phase: str | None = None
    status: str | None = None
    conditions: list[str] = []
    primary_outcomes: list[Outcome] = []
    secondary_outcomes: list[Outcome] = []
    arms: list[Arm] = []
    start_date: date | None = None
    source_record: SourceRecord | None = None    # snapshotted for provenance


def _strip_intervention(name: str) -> str:
    """v2 interventionNames look like 'Drug: Osimertinib'; keep the agent."""
    return name.split(":", 1)[1].strip() if ":" in name else name.strip()


def parse_structured(study: dict) -> StructuredTrial:
    """Pure parse of a v2 study into a StructuredTrial (outcomes + arms preserved)."""
    ps = study.get("protocolSection", {})
    ident = ps.get("identificationModule", {})
    status = ps.get("statusModule", {})
    cond = ps.get("conditionsModule", {})
    design = ps.get("designModule", {})
    outcomes = ps.get("outcomesModule", {})
    arms_mod = ps.get("armsInterventionsModule", {})

    phases = design.get("phases") or []
    primary = [Outcome(measure=o.get("measure", ""), kind="primary")
               for o in (outcomes.get("primaryOutcomes") or []) if o.get("measure")]
    secondary = [Outcome(measure=o.get("measure", ""), kind="secondary")
                 for o in (outcomes.get("secondaryOutcomes") or []) if o.get("measure")]
    arms = []
    for g in arms_mod.get("armGroups") or []:
        atype = (g.get("type") or "").upper()
        arms.append(Arm(
            label=g.get("label", ""),
            intervention_names=[_strip_intervention(n) for n in (g.get("interventionNames") or [])],
            is_comparator=atype in _COMPARATOR_ARM_TYPES,
        ))
    return StructuredTrial(
        nct_id=ident.get("nctId", ""),
        title=ident.get("briefTitle") or ident.get("officialTitle") or "",
        phase=", ".join(phases) if phases else None,
        status=status.get("overallStatus"),
        conditions=list(cond.get("conditions") or []),
        primary_outcomes=primary, secondary_outcomes=secondary, arms=arms,
        start_date=normalize_date((status.get("startDateStruct") or {}).get("date")),
    )


def fetch_trials_structured(indication: str, *, as_of: date, drug: str | None = None,
                            max_results: int = 80, client=None) -> list[StructuredTrial]:
    """Live structured fetch for an indication (optionally narrowed by drug), date-filtered
    to start < as_of. Snapshots each study for provenance. ``client`` is injectable for
    tests; the default is the Akamai-evading curl_cffi session."""
    import json as _json

    ct = ClinicalTrialsClient(client=client)
    out: list[StructuredTrial] = []
    page_token: str | None = None
    while len(out) < max_results:
        params: dict[str, object] = {
            "query.cond": indication, "pageSize": min(50, max_results - len(out)),
            "format": "json", "countTotal": "true",
        }
        if drug:
            params["query.intr"] = drug
        if page_token:
            params["pageToken"] = page_token
        resp = ct._client.get(f"{CTGOV_BASE}/studies", params=params)  # noqa: SLF001
        resp.raise_for_status()
        body = resp.json()
        for study in body.get("studies", []):
            t = parse_structured(study)
            if t.start_date is not None and t.start_date >= as_of:
                continue   # date-filter to the as-of horizon
            t.source_record = snapshot(
                _json.dumps(study).encode(), source="clinicaltrials.gov",
                source_id=t.nct_id or "NCT-unknown", doc_type=DocType.trial_registry,
                url=f"https://clinicaltrials.gov/study/{t.nct_id}", drug=drug,
                doc_date=t.start_date)
            out.append(t)
        page_token = body.get("nextPageToken")
        if not page_token:
            break
    return out[:max_results]


class ClinicalTrialsClient:
    def __init__(self, client=None) -> None:
        # Default to the Akamai-evading curl_cffi session; injectable for tests.
        self._client = client if client is not None else _default_session()

    def search(
        self,
        condition: str = "",
        *,
        intervention: str | None = None,
        status: str | None = "COMPLETED",
        page_size: int = 50,
        page_token: str | None = None,
    ) -> tuple[list[TrialRecord], SourceRecord, str | None]:
        """One page of studies. Returns (parsed, provenance snapshot, next token).

        ``intervention`` maps to ``query.intr`` (the molecule) and ``condition`` to
        ``query.cond`` (the indication) - so the corpus pulls a decision's OWN molecule's
        trials, not a global condition sweep. The snapshot is content-addressed over the
        raw response bytes.
        """
        params: dict[str, object] = {
            "pageSize": page_size,
            "countTotal": "true",
            "format": "json",
        }
        if condition:
            params["query.cond"] = condition
        if intervention:
            params["query.intr"] = intervention
        if status:
            params["filter.overallStatus"] = status
        if page_token:
            params["pageToken"] = page_token

        resp = self._client.get(f"{CTGOV_BASE}/studies", params=params)
        resp.raise_for_status()
        studies = resp.json().get("studies", [])
        parsed = [parse_study(s) for s in studies]
        rec = snapshot(
            resp.content,
            source="clinicaltrials.gov",
            source_id=f"search:intr={intervention}:cond={condition}:{status}",
            doc_type=DocType.trial_registry,
            url=str(resp.url),
        )
        next_token = resp.json().get("nextPageToken")
        return parsed, rec, next_token
