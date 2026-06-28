"""Provenance ledger: idempotent writes + fail-loud digest verification on read."""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from strata_platform.db.ledger import read_verified, record_sources
from strata_platform.db.models import SourceRecordRow
from strata_platform.substrate.contracts import DocType, SourceRecord


def _record(content: bytes) -> SourceRecord:
    return SourceRecord(
        source="pubmed", source_id="PMID:1", url="https://x", doc_type=DocType.literature,
        drug="osimertinib", doc_date=date(2024, 1, 1),
        content_sha256=SourceRecord.hash_content(content),
    )


class _FakeSnapStore:
    def __init__(self, blob: dict[str, bytes]) -> None:
        self._blob = blob

    def get(self, key: str) -> bytes:
        return self._blob[key]


def _sessionmaker():
    engine = create_engine("sqlite://")
    SourceRecordRow.__table__.create(engine)
    return sessionmaker(engine, expire_on_commit=False)


def test_record_sources_is_idempotent_on_digest() -> None:
    sm = _sessionmaker()
    rec = _record(b"hello")
    with sm() as s:
        assert record_sources(s, [rec]) == 1
    with sm() as s:
        assert record_sources(s, [rec]) == 0  # same digest → no duplicate
        rows = s.execute(select(SourceRecordRow)).scalars().all()
        assert len(rows) == 1 and rows[0].doc_type == "literature"


def test_read_verified_returns_bytes_when_digest_matches() -> None:
    content = b"the committee found the ICER highly uncertain"
    rec = _record(content)
    store = _FakeSnapStore({rec.content_sha256: content})
    assert read_verified(rec, store) == content


def test_read_verified_fails_loud_on_tamper() -> None:
    rec = _record(b"original")
    store = _FakeSnapStore({rec.content_sha256: b"tampered"})  # bytes no longer hash-match
    with pytest.raises(ValueError, match="snapshot corruption"):
        read_verified(rec, store)
