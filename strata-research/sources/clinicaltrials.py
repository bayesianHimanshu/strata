"""ClinicalTrials.gov API v2 client (Arm B surveillance / landscape input).

No auth, ~50 req/min, cursor pagination, countTotal supported. `parse_study` is a
pure function over one v2 study object and is unit-tested without the network.

CT.gov sits behind Akamai Bot Manager, which fingerprints the TLS handshake (JA3/JA4)
and 403s a plain httpx client even with a browser User-Agent. We therefore default to
a `curl_cffi` session impersonating Chrome — the same fix Phase 0 used. Any client
exposing `.get(url, params=...)` (incl. httpx) can be injected for tests.
"""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel

from core.config import CTGOV_BASE
from core.provenance import SourceRecord, normalize_date, snapshot
from sources.base import get_json


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
    """Extract a typed record from a v2 `studies[i]` object. Tolerant of missing
    fields — CTGov omits sections freely."""
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

        `intervention` maps to `query.intr` (the molecule) and `condition` to
        `query.cond` (the indication) — so the corpus can pull a decision's OWN
        molecule's trials, not a global condition sweep. The snapshot is
        content-addressed over the raw response bytes.
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
            url=str(resp.url),
            extra={"count": len(parsed)},
        )
        next_token = resp.json().get("nextPageToken")
        return parsed, rec, next_token

    def count(self, condition: str, *, status: str | None = "COMPLETED") -> int | None:
        params: dict[str, object] = {
            "query.cond": condition,
            "pageSize": 1,
            "countTotal": "true",
            "format": "json",
        }
        if status:
            params["filter.overallStatus"] = status
        return get_json(self._client, f"{CTGOV_BASE}/studies", params=params).get(
            "totalCount"
        )
