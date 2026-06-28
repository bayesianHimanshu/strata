"""Provenance primitives — promoted verbatim-in-spirit from phase0_scan.py.

Invariant #1: provenance or it doesn't exist. Every byte ingested becomes a
SourceRecord; every record is content-addressed so a run is reconstructable from
frozen inputs (invariant #7).
"""
from __future__ import annotations

import hashlib
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from core.config import SNAPSHOT_DIR
from core.contracts import DocType


class SourceRecord(BaseModel):
    """Every byte we ingest gets one of these. Auditability is non-negotiable."""

    model_config = {"frozen": True}

    source: str  # e.g. "clinicaltrials.gov", "nice"
    source_id: str  # natural id within the source (NCT id, TA id, query hash)
    url: str
    retrieved_at: datetime
    doc_date: date | None = None  # publication / decision date, normalized
    content_sha256: str
    # Corpus-composition boundary (Phase 2 Task 1): doc_type is what the document IS;
    # appraisal_id ties dossier documents to their appraisal so the synthesizer can be
    # denied an appraisal's own gold-bearing papers.
    doc_type: DocType | None = None
    appraisal_id: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("retrieved_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware (UTC)")
        return v


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def snapshot_path(digest: str, root: Path = SNAPSHOT_DIR) -> Path:
    """The content-addressed on-disk location for a digest."""
    return root / digest[:2] / digest


def snapshot(
    content: bytes,
    *,
    source: str,
    source_id: str,
    url: str,
    doc_date: date | None = None,
    doc_type: DocType | None = None,
    appraisal_id: str | None = None,
    extra: dict[str, Any] | None = None,
    root: Path = SNAPSHOT_DIR,
) -> SourceRecord:
    """Content-addressed write. Re-fetching identical content is idempotent,
    which is what makes a run reproducible."""
    digest = sha256_bytes(content)
    dest = snapshot_path(digest, root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        dest.write_bytes(content)
    return SourceRecord(
        source=source,
        source_id=source_id,
        url=url,
        retrieved_at=datetime.now(UTC),
        doc_date=doc_date,
        content_sha256=digest,
        doc_type=doc_type,
        appraisal_id=appraisal_id,
        extra=extra or {},
    )


def read_snapshot(record: SourceRecord, root: Path = SNAPSHOT_DIR) -> bytes:
    """Re-read frozen bytes by content address; verifies the digest still holds."""
    raw = snapshot_path(record.content_sha256, root).read_bytes()
    if sha256_bytes(raw) != record.content_sha256:
        raise ValueError(f"snapshot corruption: {record.content_sha256}")
    return raw



# --------------------------------------------------------------------------- #
# Date normalization. The gate for value-based column detection: NICE date cells
# arrive from openpyxl as datetime objects, so this must coerce datetime/date,
# Excel serials, ISO (+trailing time), D/M/Y and M/D/Y (day-first when ambiguous,
# since NICE is UK), and 'DD Month YYYY' text — and return None for non-dates
# (e.g. recommendation text), which is what lets detect_date_col find the column.
# --------------------------------------------------------------------------- #

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}
_EXCEL_EPOCH = datetime(1899, 12, 30)  # Excel day-0 (1900 leap bug accounted for)


def normalize_date(raw):
    """Coerce a spreadsheet/registry date value to datetime.date, or None.

    Returns None for anything that isn't a date (e.g. recommendation text) — that
    is what makes value-based column detection work.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        n = int(raw)
        if 20000 <= n <= 80000:                 # ~1954..2089: plausible serial window
            try:
                return (_EXCEL_EPOCH + timedelta(days=n)).date()
            except (OverflowError, ValueError):
                return None
        return None
    s = str(raw).strip()
    if not s:
        return None
    m = re.match(r"^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?", s)          # ISO, tolerate time
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3] or 1))
        except ValueError:
            return None
    m = re.match(r"^(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})$", s)      # D/M/Y or M/D/Y
    if m:
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        if y < 100:
            y += 2000
        if a > 12 and b <= 12:
            day, mon = a, b
        elif b > 12 and a <= 12:
            day, mon = b, a
        else:
            day, mon = a, b                       # ambiguous -> day-first (UK/NICE)
        try:
            return date(y, mon, day)
        except ValueError:
            return None
    tokens = re.findall(r"[A-Za-z]+|\d+", s)                       # 'DD Month YYYY' etc
    month = day = year = None
    for t in tokens:
        tl = t.lower()
        if tl in _MONTHS:
            month = _MONTHS[tl]
        elif t.isdigit():
            n = int(t)
            if n > 31:
                year = n
            elif day is None:
                day = n
    if month and year:
        try:
            return date(year, month, day or 1)
        except ValueError:
            return None
    return None
