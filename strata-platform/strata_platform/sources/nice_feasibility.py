"""NICE fiscal-year helpers (index-slice support for nice_index).

FINDING (research Phase 0): the NICE cancer-recommendations spreadsheet has NO decision
date — its only temporal field is "Year of Publication" as a UK fiscal year ("YYYY/YY",
e.g. 2025/26 = Apr 2025–Mar 2026). So this sheet is the INDEX of cancer TAs (universe +
categorisation + rough year), not the decision record; exact dates and the committee
rationale come from each per-TA guidance page (see nice_guidance).

Pure; no I/O.
"""
from __future__ import annotations

import re
from datetime import date

_FY = re.compile(r"^(\d{4})/(\d{2})$")
RESTRICTED = frozenset(
    {"not_recommended", "optimised", "non_submission", "only_in_research"}
)


def parse_fiscal_year(v) -> int | None:
    """'2025/26' -> 2025 (start year; FY runs Apr(start)..Mar(start+1)). None if not a
    fiscal year. The '/YY' must be the last two digits of start+1."""
    if v is None:
        return None
    m = _FY.match(str(v).strip())
    if not m:
        return None
    start = int(m.group(1))
    return start if (start + 1) % 100 == int(m.group(2)) else None


def fy_bucket(start_year: int, cutoff: date) -> str:
    """Where a fiscal year sits relative to the model cutoff. 'post' = unambiguously
    after; 'straddle' = contains the cutoff (needs per-TA dates to split); 'pre' =
    before."""
    if date(start_year + 1, 3, 31) < cutoff:
        return "pre"
    if date(start_year, 4, 1) > cutoff:
        return "post"
    return "straddle"


def detect_year_col(header: list[str], body: list[tuple]) -> int | None:
    for j, h in enumerate(header):
        if "year" in h:
            return j
    ncols = max((len(r) for r in body), default=0)
    if not ncols:
        return None
    scores = [
        sum(1 for r in body if j < len(r) and parse_fiscal_year(r[j]) is not None)
        for j in range(ncols)
    ]
    j = max(range(ncols), key=scores.__getitem__)
    return j if scores[j] >= 5 else None
