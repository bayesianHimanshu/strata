"""Real-time external-context subsystem: fetch evidence live from public sources and from
user-supplied content (URL / pasted text / uploaded file), under the leakage boundary,
with content-addressed caching so it is fresh on demand and fast on repeat.
"""
from __future__ import annotations

from strata_platform.context.connectors import (
    PUBLIC_CONNECTORS,
    ContextConnector,
    ContextQuery,
    ContextRecord,
    GenericConnector,
    UrlNotAllowed,
)
from strata_platform.context.ingest import IngestionService, IngestSummary

__all__ = [
    "PUBLIC_CONNECTORS",
    "ContextConnector",
    "ContextQuery",
    "ContextRecord",
    "GenericConnector",
    "IngestSummary",
    "IngestionService",
    "UrlNotAllowed",
]
