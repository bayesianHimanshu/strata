"""Ingestion: gather drug-scoped, leakage-date-filtered public evidence for a decision,
chunk it (structure_aware_prefixed), embed it, and write it to the retrieval store -
guarded by a fail-loud health gate so a blob / wrong-drug / empty-literature corpus can
never silently produce a fake retrieval result.
"""
from __future__ import annotations

from strata_platform.ingest.corpus import (
    RetrievableDoc,
    build_corpus,
    clean_query,
    compute_sibling_map,
    pubmed_query,
)
from strata_platform.ingest.health import CorpusHealthError, assert_corpus_healthy, composition

__all__ = [
    "CorpusHealthError",
    "RetrievableDoc",
    "assert_corpus_healthy",
    "build_corpus",
    "clean_query",
    "composition",
    "compute_sibling_map",
    "pubmed_query",
]
