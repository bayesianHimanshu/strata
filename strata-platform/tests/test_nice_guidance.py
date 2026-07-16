"""Per-TA guidance: pure parser (fixtures) + resilient fetcher (injected HTTP)."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from strata_platform.sources.nice_guidance import (
    NICEGuidanceClient,
    guidance_url,
    is_withdrawn,
    parse_guidance,
)
from strata_platform.substrate.contracts import DocType

FIX = Path(__file__).parent / "fixtures" / "nice"


def _html(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# --- pure parser (fixture-backed) ------------------------------------------- #


def test_parse_guidance_time_element() -> None:
    pg = parse_guidance(_html("ta1000.html"), "TA1000")
    assert pg.published_date == date(2026, 1, 15)
    assert pg.title.startswith("Pembrolizumab for untreated")
    low = pg.rationale_raw.lower()
    assert "immature" in low and "comparator" in low and "icer" in low
    assert "Information about pembrolizumab" not in pg.rationale_raw


def test_parse_guidance_published_text_and_plain_heading() -> None:
    pg = parse_guidance(_html("ta1002.html"), "TA1002")
    assert pg.published_date == date(2026, 3, 3)
    assert "not recommended" in pg.rationale_raw.lower()
    assert "indirect comparison" in pg.rationale_raw.lower()
    assert "The technology" not in pg.rationale_raw


def test_parse_guidance_fail_loud_missing_date() -> None:
    html = "<h1>x</h1><h2>1 Recommendations</h2><p>data were immature</p>"
    with pytest.raises(ValueError, match="no published date"):
        parse_guidance(html, "TA9")


def test_parse_guidance_fail_loud_empty_rationale() -> None:
    html = '<time datetime="2026-01-01">1 Jan 2026</time><p>nothing structured</p>'
    with pytest.raises(ValueError, match="empty rationale"):
        parse_guidance(html, "TA9")


def test_parse_guidance_real_chapter_skips_nav_toc() -> None:
    pg = parse_guidance(_html("ta_chapter.html"), "TA1064")
    assert pg.published_date == date(2025, 5, 22)
    low = pg.rationale_raw.lower()
    assert "1.1 dostarlimab" in low
    assert "immature" in low and "indirect comparison" in low and "icer" in low
    assert "Marketing authorisation" not in pg.rationale_raw
    assert len(pg.rationale_raw) > 200


def test_is_withdrawn_detects_replacement_notice() -> None:
    assert is_withdrawn(_html("ta_withdrawn.html")) is True
    assert is_withdrawn(_html("ta_chapter.html")) is False


# --- resilient fetcher (injected HTTP, no network) -------------------------- #


def _fetcher(tmp_path: Path, responses: dict[str, tuple[int, bytes, str]]):
    calls: list[str] = []

    def fake_get(url: str) -> tuple[int, bytes, str]:
        calls.append(url)
        return responses[url]

    client = NICEGuidanceClient(
        http_get=fake_get, cache_dir=tmp_path, sleeper=lambda _s: None
    )
    return client, calls


def _url(ta: str) -> str:
    return guidance_url(ta)


def test_fetch_ok_tags_provenance_and_boundary(tmp_path: Path) -> None:
    html = _html("ta1000.html").encode("utf-8")
    client, calls = _fetcher(tmp_path, {_url("TA1000"): (200, html, "text/html")})
    res = client.fetch("TA1000")
    assert res.status == "ok"
    assert res.parsed.published_date == date(2026, 1, 15)
    rec = res.source_record
    assert rec.doc_type == DocType.ta_final_guidance  # gold-bearing -> excluded
    assert rec.appraisal_id == "TA1000"
    assert rec.doc_date == date(2026, 1, 15)
    assert len(calls) == 1


def test_fetch_is_idempotent_cache(tmp_path: Path) -> None:
    html = _html("ta1000.html").encode("utf-8")
    client, calls = _fetcher(tmp_path, {_url("TA1000"): (200, html, "text/html")})
    r1 = client.fetch("TA1000")
    r2 = client.fetch("TA1000")  # served from disk, no second network call
    assert len(calls) == 1
    assert r2.status == "ok"
    assert r2.source_record.content_sha256 == r1.source_record.content_sha256
    assert r2.parsed == r1.parsed


def test_fetch_404_is_graceful_and_cached(tmp_path: Path) -> None:
    client, calls = _fetcher(tmp_path, {_url("TA9999"): (404, b"", "")})
    res = client.fetch("TA9999")
    assert res.status == "unavailable"
    assert "404" in res.reason
    client.fetch("TA9999")  # cached miss
    assert len(calls) == 1


def test_fetch_block_page_is_unavailable(tmp_path: Path) -> None:
    resp = {_url("TA1000"): (200, b"Access Denied", "text/plain")}
    client, _ = _fetcher(tmp_path, resp)
    res = client.fetch("TA1000")
    assert res.status == "unavailable"
    assert "non-HTML" in res.reason


def test_fetch_withdrawn_is_graceful_not_fail_loud(tmp_path: Path) -> None:
    html = _html("ta_withdrawn.html").encode("utf-8")
    client, _ = _fetcher(tmp_path, {_url("TA963"): (200, html, "text/html")})
    res = client.fetch("TA963")
    assert res.status == "unavailable"
    assert "withdrawn or replaced" in res.reason


def test_fetch_fail_loud_on_unparseable_200(tmp_path: Path) -> None:
    bad = b"<html><h2>1 Recommendations</h2><p>data were immature</p></html>"
    client, _ = _fetcher(tmp_path, {_url("TA1000"): (200, bad, "text/html")})
    with pytest.raises(ValueError, match="no published date"):
        client.fetch("TA1000")
