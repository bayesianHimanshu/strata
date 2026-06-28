"""Provenance ledger — the append-only record of every byte ingested.

Each ``SourceRecord`` becomes a ``SourceRecordRow`` keyed by its content SHA-256 (the
ALCOA+ audit substrate, 21 CFR Part 11). Writes are idempotent on the digest. Reads verify
the digest against the snapshot store before returning bytes, so silent corruption (or a
swapped snapshot) fails loud rather than feeding a wrong document into a grounded claim.
"""
from __future__ import annotations

from strata_platform.substrate.contracts import SourceRecord
from strata_platform.substrate.provenance import SnapshotStore


def record_sources(session, records: list[SourceRecord]) -> int:
    """Idempotently persist provenance rows. Returns the number newly written."""
    from strata_platform.db.models import SourceRecordRow

    written = 0
    for r in records:
        existing = session.get(SourceRecordRow, r.content_sha256)
        if existing is not None:
            continue
        session.add(SourceRecordRow(
            content_sha256=r.content_sha256, source=r.source, source_id=r.source_id,
            url=r.url, doc_type=r.doc_type.value if r.doc_type else None,
            appraisal_id=r.appraisal_id, drug=r.drug, doc_date=r.doc_date,
            fetched_at=r.fetched_at,
        ))
        written += 1
    session.commit()
    return written


def read_verified(record: SourceRecord, store: SnapshotStore | None = None) -> bytes:
    """Re-read snapshot bytes by content address; raise if the digest no longer holds."""
    store = store or SnapshotStore()
    raw = store.get(record.content_sha256)
    if SourceRecord.hash_content(raw) != record.content_sha256:
        raise ValueError(
            f"snapshot corruption: digest mismatch for {record.content_sha256}"
        )
    return raw
