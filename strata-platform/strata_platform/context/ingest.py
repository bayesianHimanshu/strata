"""Ingestion service: fetch via connectors, dedup, embed once, upsert, honour a freshness
TTL. Three caching layers, all fail-soft:

  * content-addressed snapshots — same bytes → same SHA → never re-stored;
  * embed-once — a record whose content hash is already indexed is skipped (no re-embed);
  * a fetch ledger — (drug, indication, connector) → fetched_at, enforcing a TTL so a
    repeat within the window serves from the index without re-hitting the API.

A connector error degrades that source to zero with a recorded error; it never aborts the
run.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel

from strata_platform.context.connectors import (
    ContextQuery,
    ContextRecord,
    GenericConnector,
)


class IngestSummary(BaseModel):
    per_connector: dict[str, dict]
    total_chunks: int
    total_new: int


class InMemoryFetchLedger:
    """(drug, indication, connector) -> last fetch time + count. Process-local; the prod
    swap is a small DB table (same interface)."""

    def __init__(self) -> None:
        self._t: dict[tuple, tuple[datetime, int]] = {}

    def was_recent(self, key: tuple, ttl_hours: int) -> bool:
        rec = self._t.get(key)
        if rec is None:
            return False
        age = (datetime.now(timezone.utc) - rec[0]).total_seconds()
        return age < ttl_hours * 3600

    def record(self, key: tuple, count: int) -> None:
        self._t[key] = (datetime.now(timezone.utc), count)


def _emit(progress, connector: str, state: str, count: int = 0) -> None:
    if progress is not None:
        progress({"connector": connector, "state": state, "count": count})


class IngestionService:
    def __init__(self, store, embedder, connectors: dict, *,
                 freshness_ttl_hours: int = 24, ledger=None, seen=None) -> None:
        self.store = store
        self.embedder = embedder
        self.connectors = connectors            # {key: ContextConnector instance}
        self._ttl = freshness_ttl_hours
        self._ledger = ledger or InMemoryFetchLedger()
        self._indexed: set[str] = seen if seen is not None else set()

    def _index_record(self, r: ContextRecord) -> int:
        """Embed (once) + upsert a record's chunks; returns new chunks written."""
        sha = r.source_record.content_sha256
        if sha in self._indexed or not r.chunks:
            return 0
        vectors = self.embedder.embed([c.text for c in r.chunks])   # embed ONCE
        new = self.store.upsert(r.chunks, vectors)
        self._indexed.add(sha)
        return new

    def ingest(self, q: ContextQuery, *, connectors: list[str],
               progress=None) -> IngestSummary:
        per: dict[str, dict] = {}
        total_chunks = total_new = 0
        for key in connectors:
            _emit(progress, key, "queued")
            lkey = (q.drug.lower(), (q.indication or "").lower(), key)
            if self._ledger.was_recent(lkey, self._ttl):
                per[key] = {"fetched": 0, "new": 0, "cached": 1, "errors": 0}
                _emit(progress, key, "done", 0)
                continue
            _emit(progress, key, "fetching")
            try:
                records = self.connectors[key].fetch(q)
            except Exception as exc:  # noqa: BLE001 - one source must not abort the run
                per[key] = {"fetched": 0, "new": 0, "cached": 0, "errors": 1,
                            "error": str(exc)}
                _emit(progress, key, "error")
                continue
            new = chunks_n = 0
            for r in records:
                added = self._index_record(r)
                new += added
                chunks_n += len(r.chunks)
            self._ledger.record(lkey, len(records))
            per[key] = {"fetched": len(records), "new": new, "cached": 0, "errors": 0}
            total_chunks += chunks_n
            total_new += new
            _emit(progress, key, "done", len(records))
        return IngestSummary(per_connector=per, total_chunks=total_chunks,
                             total_new=total_new)

    def add_external(self, *, kind: str, value, drug: str, indication: str | None,
                     doc_date, filename: str | None = None,
                     url_fetcher=None) -> ContextRecord:
        """Generic connector: ingest user-supplied URL / text / file immediately."""
        rec = GenericConnector().add(kind=kind, value=value, drug=drug,
                                     indication=indication, doc_date=doc_date,
                                     filename=filename, url_fetcher=url_fetcher)
        self._index_record(rec)
        return rec
