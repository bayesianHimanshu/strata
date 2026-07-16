"""Task 2.5: the Arm A dataset pipeline, end-to-end and offline."""
from __future__ import annotations

import io
import json
from datetime import date
from pathlib import Path

import openpyxl

from core.contracts import VulnCategory
from experiments.build_arm_a_dataset import build_arm_a_dataset
from sources.nice_guidance import NICEGuidanceClient, guidance_url

FIX = Path(__file__).parent / "fixtures" / "nice"

HEADER = ["TA ID", "Year of Publication", "Type", "Technology", "Route", "Indication",
          "Categorisation"]
ROWS = [
    ["TA1000", "2024/25", "single", "Pembrolizumab", "iv", "NSCLC 1L", "Optimised"],
    ["TA1001", "2024/25", "multi", "Nivolumab", "iv", "Melanoma", "Recommended"],
    ["TA1002", "2025/26", "single", "Osimertinib", "po", "NSCLC", "Not recommended"],
    ["TA1003", "2025/26", "single", "Drug D", "iv", "RCC", "Recommended"],
    ["TA1004", "2026/27", "single", "Drug E", "oral", "Myeloma", "Optimised"],
    ["TA1005", "2026/27", "multi", "Drug F", "iv", "Lymphoma", "Recommended"],
    ["TA1006", "2024/25", "single", "Drug G", "iv", "Breast", "Recommended"],
]


def _index_xlsx() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Cancer recommendations"
    ws.append(HEADER)
    for r in ROWS:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _fixture_client(tmp_path: Path) -> NICEGuidanceClient:
    # Only TA1000 and TA1002 have guidance fixtures; everything else 404s.
    available = {
        guidance_url("TA1000"): (FIX / "ta1000.html").read_bytes(),
        guidance_url("TA1002"): (FIX / "ta1002.html").read_bytes(),
    }

    def fake_get(url: str) -> tuple[int, bytes, str]:
        if url in available:
            return 200, available[url], "text/html"
        return 404, b"", ""

    return NICEGuidanceClient(
        http_get=fake_get, snapshot_root=tmp_path / "snap", sleeper=lambda _s: None
    )


def test_pipeline_assembles_decisions_and_gold(tmp_path: Path) -> None:
    out = tmp_path / "arm_a"
    decisions, gold = build_arm_a_dataset(
        _index_xlsx(),
        cutoff=date(2026, 2, 1),
        guidance_client=_fixture_client(tmp_path),
        out_dir=out,
        model_cutoff=date(2026, 2, 1),
    )
    by_id = {d.decision_id: d for d in decisions}
    assert set(by_id) == {"TA1000", "TA1002"}  # only the two fetchable TAs

    # exact published date OVERRIDES the year-granular index
    assert by_id["TA1000"].decision_date == date(2026, 1, 15)
    assert by_id["TA1002"].decision_date == date(2026, 3, 3)
    # fields carried from the index
    assert by_id["TA1000"].drug == "Pembrolizumab"
    assert by_id["TA1000"].outcome == "optimised"
    assert by_id["TA1000"].appraisal_id == "TA1000"
    assert by_id["TA1000"].rationale_raw  # gold-bearing text present

    # candidate gold mined from rationale
    assert VulnCategory.comparator in gold["TA1002"]
    assert VulnCategory.icer_uncertainty in gold["TA1002"]


def test_pipeline_manifest_reports_clean_arm_size(tmp_path: Path) -> None:
    out = tmp_path / "arm_a"
    build_arm_a_dataset(
        _index_xlsx(),
        cutoff=date(2026, 2, 1),
        guidance_client=_fixture_client(tmp_path),
        out_dir=out,
        model_cutoff=date(2026, 2, 1),
    )
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["total_fetched"] == 2
    assert manifest["unavailable"] == 5
    assert manifest["recent_slice_size"] == 7
    # TA1000 published 2026-01-15 (pre), TA1002 published 2026-03-03 (post)
    assert manifest["n_pre"] == 1
    assert manifest["n_post"] == 1
    assert manifest["n_post_restricted"] == 1  # TA1002 'not_recommended'
    assert manifest["gold_category_coverage"]["comparator"] == 2


def test_pipeline_records_parse_failure_without_crashing(tmp_path: Path) -> None:
    # One TA returns a reachable 200 page the parser cannot read (no date). The batch
    # must record it in the manifest and still process the good TAs - not abort.
    available = {
        guidance_url("TA1000"): (200, (FIX / "ta1000.html").read_bytes(), "text/html"),
        guidance_url("TA1002"): (200, b"<html><h2>1 Recommendation</h2><p>x</p></html>",
                                 "text/html"),  # 200, HTML, but no published date
    }

    def fake_get(url: str) -> tuple[int, bytes, str]:
        return available.get(url, (404, b"", ""))

    client = NICEGuidanceClient(
        http_get=fake_get, snapshot_root=tmp_path / "snap", sleeper=lambda _s: None
    )
    out = tmp_path / "arm_a"
    decisions, _ = build_arm_a_dataset(
        _index_xlsx(), cutoff=date(2026, 2, 1), guidance_client=client, out_dir=out,
    )
    assert {d.decision_id for d in decisions} == {"TA1000"}  # good TA still processed
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["parse_failed"] == 1
    assert manifest["parse_failed_ids"] == ["TA1002"]


def test_pipeline_persists_jsonable_gold(tmp_path: Path) -> None:
    out = tmp_path / "arm_a"
    build_arm_a_dataset(
        _index_xlsx(),
        cutoff=date(2026, 2, 1),
        guidance_client=_fixture_client(tmp_path),
        out_dir=out,
    )
    gold = json.loads((out / "candidate_gold.json").read_text())
    # values are plain VulnCategory.value strings (closed_book_probe consumes these)
    assert all(isinstance(v, list) for v in gold.values())
    assert all(isinstance(s, str) for v in gold.values() for s in v)
    valid = {c.value for c in VulnCategory}
    assert all(s in valid for v in gold.values() for s in v)
    decisions = json.loads((out / "decisions.json").read_text())
    assert decisions[0]["decision_date"] == "2026-01-15"  # ISO date string