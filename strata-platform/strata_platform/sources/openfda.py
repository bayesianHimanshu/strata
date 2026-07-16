"""openFDA client - drug labels + FAERS adverse events (safety signal + label corpus).

openFDA returns a ``meta.results.total`` count and a ``results`` array. Parsing is split
out so the count/extraction logic is testable offline. The retrieval corpus also needs the
label TEXT (indications + warnings) and its effective date for leakage filtering -
``parse_label_docs`` provides that as a pure function.

Hard-won fixes carried over:
  - ``404`` means "no matches" = treat as an empty result, NOT an error.
  - Query labels by ``generic_name`` (exact) - never fall back to a default/first doc
    (that was the "ORSERDU label on every drug" wrong-drug bug).
  - An API key (when configured) raises the rate limits.
"""
from __future__ import annotations

from datetime import date

import httpx
from pydantic import BaseModel

from strata_platform.config import get_settings
from strata_platform.sources.base import build_client
from strata_platform.sources.dates import normalize_date
from strata_platform.sources.endpoints import OPENFDA_BASE
from strata_platform.substrate.contracts import DocType, SourceRecord
from strata_platform.substrate.provenance import snapshot


class FDAResult(BaseModel):
    model_config = {"frozen": True}

    query: str
    total: int | None
    n_results: int


class LabelDoc(BaseModel):
    model_config = {"frozen": True}

    brand: str
    generic: str
    text: str  # indications + warnings - the retrievable label content
    effective_date: date | None = None


def parse_meta(query: str, payload: dict) -> FDAResult:
    total = (payload.get("meta", {}).get("results", {}) or {}).get("total")
    return FDAResult(
        query=query, total=total, n_results=len(payload.get("results", []) or [])
    )


def _first(value) -> str:
    """openFDA label fields are arrays of strings; take and join them."""
    if isinstance(value, list):
        return " ".join(str(v) for v in value if v)
    return str(value or "")


def _effective_date(raw) -> date | None:
    """openFDA effective_time is compact 'YYYYMMDD'; normalize via dashed ISO."""
    s = _first(raw).strip()
    if len(s) == 8 and s.isdigit():
        return normalize_date(f"{s[:4]}-{s[4:6]}-{s[6:8]}")
    return normalize_date(s)


def parse_label_docs(payload: dict) -> list[LabelDoc]:
    """Pure parse of a drug/label.json body into retrievable LabelDocs."""
    out: list[LabelDoc] = []
    for r in payload.get("results", []) or []:
        openfda = r.get("openfda", {}) or {}
        text = " ".join(
            t
            for t in (
                _first(r.get("indications_and_usage")),
                _first(r.get("warnings_and_cautions") or r.get("warnings")),
            )
            if t
        ).strip()
        if not text:
            continue
        out.append(
            LabelDoc(
                brand=_first(openfda.get("brand_name")),
                generic=_first(openfda.get("generic_name")),
                text=text,
                effective_date=_effective_date(r.get("effective_time")),
            )
        )
    return out


class OpenFDAClient:
    def __init__(self, client: httpx.Client | None = None,
                 api_key: str | None = None) -> None:
        self._client = client or build_client()
        self._api_key = api_key if api_key is not None else get_settings().openfda_api_key

    def _params(self, search: str, limit: int) -> dict:
        p: dict = {"search": search, "limit": limit}
        if self._api_key:
            p["api_key"] = self._api_key
        return p

    def _query(
        self, endpoint: str, search: str, limit: int
    ) -> tuple[FDAResult, SourceRecord]:
        resp = self._client.get(
            f"{OPENFDA_BASE}/{endpoint}", params=self._params(search, limit)
        )
        if resp.status_code == 404:
            payload: dict = {"results": []}
        else:
            resp.raise_for_status()
            payload = resp.json()
        result = parse_meta(search, payload)
        rec = snapshot(
            resp.content,
            source="openfda",
            source_id=f"{endpoint}:{search}",
            doc_type=DocType.faers if "event" in endpoint else DocType.label,
            url=str(resp.url),
        )
        return result, rec

    def faers_events(
        self, search: str, *, limit: int = 1
    ) -> tuple[FDAResult, SourceRecord]:
        return self._query("drug/event.json", search, limit)

    def fetch_events(self, search: str, *, limit: int = 100, skip: int = 0) -> dict:
        """Raw FAERS event payload (results[]) for signal ingestion. 404 ('no matches') ->
        empty results, not an error. openFDA caps ``limit`` at 1000."""
        params = self._params(search, min(limit, 1000))
        params["skip"] = skip
        resp = self._client.get(f"{OPENFDA_BASE}/drug/event.json", params=params)
        if resp.status_code == 404:
            return {"results": []}
        resp.raise_for_status()
        return resp.json()

    def labels(self, search: str, *, limit: int = 1) -> tuple[FDAResult, SourceRecord]:
        return self._query("drug/label.json", search, limit)

    def fetch_label_docs(
        self, search: str, *, limit: int = 1
    ) -> tuple[list[LabelDoc], SourceRecord]:
        """Fetch label TEXT (indications + warnings) + effective date for the corpus.
        404 ('no matches') is treated as an empty result, not an error."""
        resp = self._client.get(
            f"{OPENFDA_BASE}/drug/label.json", params=self._params(search, limit)
        )
        if resp.status_code == 404:
            payload: dict = {"results": []}
        else:
            resp.raise_for_status()
            payload = resp.json()
        docs = parse_label_docs(payload)
        rec = snapshot(
            resp.content,
            source="openfda",
            source_id=f"label:{search}",
            doc_type=DocType.label,
            url=str(resp.url),
        )
        return docs, rec
