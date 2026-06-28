"""Task 2.1: recent-slice cancer index reader (offline workbook fixture)."""
from __future__ import annotations

import io
from datetime import date

import openpyxl

from sources.nice_index import detect_ta_col, recent_cancer_tas

# Real layout: TA ID(0) Year(1) Type(2) Technology(3) _(4) Indication(5) Categorisation(6)
HEADER = ["TA ID", "Year of Publication", "Type", "Technology", "Route", "Indication",
          "Categorisation"]
ROWS = [
    ["TA003", "2018/19", "single", "Old Drug", "oral", "NSCLC", "Recommended"],
    ["TA900", "2023/24", "single", "Older Drug", "iv", "Melanoma", "Recommended"],
    ["TA1000", "2024/25", "single", "Pembrolizumab", "iv", "NSCLC 1L", "Optimised"],
    ["TA1001", "2024/25", "multi", "Nivolumab", "iv", "Melanoma", "Recommended"],
    ["TA1002", "2025/26", "single", "Osimertinib", "po", "NSCLC", "Not recommended"],
    ["TA1003", "2025/26", "single", "Drug D", "iv", "RCC", "Recommended"],
    ["TA1004", "2026/27", "single", "Drug E", "oral", "Myeloma", "Optimised"],
    ["TA1005", "2026/27", "multi", "Drug F", "iv", "Lymphoma", "Recommended"],
    ["TA1006", "2024/25", "single", "Drug G", "iv", "Breast", "Recommended"],
]


def _workbook(header: list, rows: list[list]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Cancer recommendations"
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_detect_ta_col_by_value_regex() -> None:
    body = [tuple(r) for r in ROWS]
    assert detect_ta_col(body) == 0


def test_recent_slice_filters_and_types() -> None:
    refs = recent_cancer_tas(_workbook(HEADER, ROWS), date(2026, 2, 1))
    ids = {r.ta_id for r in refs}
    # FY >= 2024 kept; TA003 (2018) and TA900 (2023) dropped
    assert ids == {"TA1000", "TA1001", "TA1002", "TA1003", "TA1004", "TA1005", "TA1006"}
    by_id = {r.ta_id: r for r in refs}
    assert by_id["TA1000"].fiscal_year_start == 2024
    assert by_id["TA1000"].technology == "Pembrolizumab"
    assert by_id["TA1000"].indication == "NSCLC 1L"
    assert by_id["TA1000"].categorisation == "optimised"  # classified label
    assert by_id["TA1002"].categorisation == "not_recommended"


def test_cutoff_window_moves_with_year() -> None:
    # A later cutoff narrows the slice (min_year = cutoff.year - 2).
    refs = recent_cancer_tas(_workbook(HEADER, ROWS), date(2027, 2, 1))  # min_year 2025
    assert {r.ta_id for r in refs} == {"TA1002", "TA1003", "TA1004", "TA1005"}
