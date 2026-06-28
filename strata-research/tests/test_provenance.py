"""Invariants #1 and #7: provenance, content addressing, reproducibility."""
from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.provenance import (
    SourceRecord,
    normalize_date,
    read_snapshot,
    sha256_bytes,
    snapshot,
    snapshot_path,
)


def test_content_addressing_is_idempotent(tmp_path: Path) -> None:
    r1 = snapshot(b"hello", source="t", source_id="1", url="u", root=tmp_path)
    r2 = snapshot(b"hello", source="t", source_id="1", url="u", root=tmp_path)
    assert r1.content_sha256 == r2.content_sha256 == sha256_bytes(b"hello")
    assert snapshot_path(r1.content_sha256, tmp_path).exists()


def test_snapshot_roundtrip_verifies_digest(tmp_path: Path) -> None:
    rec = snapshot(b"payload", source="t", source_id="1", url="u", root=tmp_path)
    assert read_snapshot(rec, tmp_path) == b"payload"


def test_corrupted_snapshot_is_detected(tmp_path: Path) -> None:
    rec = snapshot(b"payload", source="t", source_id="1", url="u", root=tmp_path)
    snapshot_path(rec.content_sha256, tmp_path).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="corruption"):
        read_snapshot(rec, tmp_path)


def test_source_record_is_frozen() -> None:
    rec = SourceRecord(
        source="s",
        source_id="i",
        url="u",
        retrieved_at=datetime.now(UTC),
        content_sha256="x",
    )
    with pytest.raises(ValidationError):
        rec.source = "mutated"  # type: ignore[misc]


def test_retrieved_at_must_be_tz_aware() -> None:
    with pytest.raises(ValidationError):
        SourceRecord(
            source="s",
            source_id="i",
            url="u",
            retrieved_at=datetime(2026, 1, 1),  # naive
            content_sha256="x",
        )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-03-14", date(2026, 3, 14)),
        ("2026-03", date(2026, 3, 1)),
        ("January 2024", date(2024, 1, 1)),
        ("15 January 2024", date(2024, 1, 15)),
        ("January 15, 2024", date(2024, 1, 15)),
        ("not a date", None),
        (None, None),
        ("", None),
    ],
)
def test_normalize_date(raw: str | None, expected: date | None) -> None:
    assert normalize_date(raw) == expected
