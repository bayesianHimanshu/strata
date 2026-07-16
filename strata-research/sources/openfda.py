from __future__ import annotations

from datetime import date

import httpx
from pydantic import BaseModel

from core.config import OPENFDA_BASE
from core.provenance import SourceRecord, normalize_date, snapshot
from sources.base import build_client


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
    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or build_client()

    def _query(
        self, endpoint: str, search: str, limit: int
    ) -> tuple[FDAResult, SourceRecord]:
        resp = self._client.get(
            f"{OPENFDA_BASE}/{endpoint}", params={"search": search, "limit": limit}
        )
        resp.raise_for_status()
        result = parse_meta(search, resp.json())
        rec = snapshot(
            resp.content,
            source="openfda",
            source_id=f"{endpoint}:{search}",
            url=str(resp.url),
            extra={"total": result.total},
        )
        return result, rec

    def faers_events(
        self, search: str, *, limit: int = 1
    ) -> tuple[FDAResult, SourceRecord]:
        return self._query("drug/event.json", search, limit)

    def labels(self, search: str, *, limit: int = 1) -> tuple[FDAResult, SourceRecord]:
        return self._query("drug/label.json", search, limit)

    def fetch_label_docs(
        self, search: str, *, limit: int = 1
    ) -> tuple[list[LabelDoc], SourceRecord]:
        """Fetch label TEXT (indications + warnings) + effective date for the corpus.
        404 ('no matches') is treated as an empty result, not an error (Phase 0 fix)."""
        resp = self._client.get(
            f"{OPENFDA_BASE}/drug/label.json", params={"search": search, "limit": limit}
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
            url=str(resp.url),
            extra={"n": len(docs)},
        )
        return docs, rec
