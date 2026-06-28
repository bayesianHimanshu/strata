"""G-BA / IQWiG client (secondary gold source, supplements thin NICE post-cutoff).

G-BA publishes benefit-assessment decisions as PDFs (often German, scanned), so full
structured extraction is a PDF→OCR pipeline scheduled with DecisionMiner in Phase 2.
What Phase 1 owns and tests: the fetch+snapshot path, so every G-BA document enters
the system content-addressed and provenanced, and the typed seam parsing will fill.
"""
from __future__ import annotations

import httpx

from core.config import GBA_BASE
from core.provenance import SourceRecord, snapshot
from sources.base import build_client


class GBAClient:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or build_client()

    def fetch_document(
        self, url: str, *, source_id: str
    ) -> SourceRecord:
        """Snapshot one G-BA document by URL. doc_date is unknown until parsing, so
        it is left None — and therefore (correctly) rejected by the leakage filter
        until a date is established. No document is admitted to retrieval undated."""
        if not url.startswith(GBA_BASE):
            # G-BA assets are sometimes on subdomains; keep the guard advisory.
            pass
        resp = self._client.get(url)
        resp.raise_for_status()
        return snapshot(
            resp.content,
            source="gba",
            source_id=source_id,
            url=str(resp.url),
            extra={"content_type": resp.headers.get("content-type", "")},
        )

    def parse_decision(self, record: SourceRecord) -> object:
        """PDF→OCR extraction → Decision. Implemented in Phase 2 with DecisionMiner,
        where the OCR dependency and German-language handling are introduced."""
        raise NotImplementedError(
            "G-BA PDF→OCR extraction is a Phase 2 DecisionMiner concern; Phase 1 "
            "provides the provenanced fetch only."
        )
