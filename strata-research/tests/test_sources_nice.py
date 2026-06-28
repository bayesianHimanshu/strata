"""NICE pure parsers: classifier, xlsx-link finder, workbook -> Decision[]."""
from __future__ import annotations

import io
from datetime import date

import openpyxl

from core.contracts import Decision
from sources.nice import (
    classify_recommendation,
    find_xlsx_url,
    parse_workbook,
    resolve_clean_arm,
)


def test_classifier_orders_negative_before_generic() -> None:
    assert classify_recommendation("Not recommended") == "not_recommended"
    assert classify_recommendation("Recommended for the Cancer Drugs Fund") == "cdf"
    assert classify_recommendation("Optimised") == "optimised"
    assert classify_recommendation("Non-submission") == "non_submission"
    assert classify_recommendation("Only in research") == "only_in_research"
    assert classify_recommendation("OIR") == "only_in_research"
    assert classify_recommendation("Recommended") == "recommended"
    assert classify_recommendation("") == "other"


def test_resolve_clean_arm_stratifies_exactly_around_cutoff() -> None:
    cutoff = date(2026, 2, 1)
    decisions = [
        Decision(agency="NICE", decision_id="TA1", decision_date=date(2026, 3, 1),
                 indication="x", outcome="not_recommended", appraisal_id="TA1"),
        Decision(agency="NICE", decision_id="TA2", decision_date=date(2026, 4, 1),
                 indication="x", outcome="optimised", appraisal_id="TA2"),
        Decision(agency="NICE", decision_id="TA3", decision_date=date(2026, 3, 1),
                 indication="x", outcome="recommended", appraisal_id="TA3"),
        Decision(agency="NICE", decision_id="TA4", decision_date=date(2026, 2, 1),
                 indication="x", outcome="not_recommended", appraisal_id="TA4"),
        Decision(agency="NICE", decision_id="TA5", decision_date=date(2025, 1, 1),
                 indication="x", outcome="not_recommended", appraisal_id="TA5"),
    ]
    out = resolve_clean_arm(decisions, cutoff)
    assert out["post_cutoff"] == 3
    assert out["pre_cutoff"] == 2  # on-cutoff TA4 counts as pre (strict >)
    assert out["post_cutoff_restricted"] == 2
    assert out["post_cutoff_restricted_ids"] == ["TA1", "TA2"]
    assert out["clean_arm_self_sufficient"] is False  # 2 < 12


def test_find_xlsx_url_resolves_relative() -> None:
    assert (
        find_xlsx_url('<a href="/media/cancer-recs.xlsx">x</a>')
        == "https://www.nice.org.uk/media/cancer-recs.xlsx"
    )
    assert find_xlsx_url("<a href='nope.pdf'>x</a>") is None


def _workbook(rows: list[list]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Recommendations"
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_parse_workbook_builds_dated_decisions() -> None:
    # Value-based column detection needs >= MIN_COL_HITS (5) dated/classifiable rows,
    # so the fixture carries a realistic block of rows rather than a token few.
    rows = [["TA number", "Title", "Recommendation", "Publication date"]]
    for i in range(6):
        rows.append(
            [f"TA20{i:02d}", f"Drug {i} for NSCLC", "Recommended", f"2025-0{i + 1}-15"]
        )
    rows.append(["TA1000", "Drug A for NSCLC", "Not recommended", "15 January 2026"])
    rows.append(["TA1001", "Drug B for melanoma", "Optimised", "2026-03"])
    rows.append(["TA1002", "Drug C", "Optimised", None])  # undated -> skipped

    decisions = parse_workbook(_workbook(rows))
    assert all(isinstance(d, Decision) for d in decisions)
    by_id = {d.decision_id: d for d in decisions}
    assert "TA1002" not in by_id  # undated row dropped
    assert by_id["TA1000"].decision_date == date(2026, 1, 15)
    assert by_id["TA1000"].outcome == "not_recommended"
    assert by_id["TA1001"].decision_date == date(2026, 3, 1)
    assert by_id["TA1001"].outcome == "optimised"
