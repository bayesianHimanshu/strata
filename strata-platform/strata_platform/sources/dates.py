"""Date normalization - ported verbatim-in-spirit from the STRATA research repo.

The gate for value-based column detection: NICE date cells arrive from openpyxl as
datetime objects, registry dates as ISO/compact strings, guidance pages as 'DD Month
YYYY' text. This coerces all of them to ``datetime.date`` and returns ``None`` for
anything that isn't a date (e.g. recommendation prose) - which is exactly what lets the
column detectors find the date column by content rather than by header name.

Pure; no I/O.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}
_EXCEL_EPOCH = datetime(1899, 12, 30)  # Excel day-0 (1900 leap bug accounted for)


def normalize_date(raw) -> date | None:
    """Coerce a spreadsheet/registry/page date value to ``datetime.date``, or ``None``.

    Returns ``None`` for anything that isn't a date - that is what makes value-based
    column detection work.
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
