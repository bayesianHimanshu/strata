"""Per-TA NICE guidance fetcher + pure parser (Task 2.2).

The spreadsheet gives the universe at fiscal-year granularity; the per-TA guidance
page gives the EXACT decision date and the committee rationale (the gold-bearing
text). One fetch serves both.

Two halves, deliberately separated:

  * `parse_guidance(html, ta_id)` — a PURE function, unit-tested against saved HTML
    fixtures under tests/fixtures/nice/. Fails loud on a missing published date or an
    empty rationale: we cannot stratify or mine gold from a page we could not read.

  * `NICEGuidanceClient.fetch(ta_id)` — the resilient, side-effecting fetch. Carries
    the Phase 0 lessons: browser UA, retry-with-backoff on 5xx, escalate to curl_cffi
    on 403 (the page may sit behind the same WAF as ClinicalTrials.gov), and a typed
    `unavailable` result on 404/withdrawn so a single dead TA never crashes the batch.
    Snapshots are content-addressed and the per-TA pointer is the cache: a TA already
    fetched is served from disk with no network call.

The HTTP itself is injected (`http_get`) so the whole client is testable with no
network — the default implementation is the only thing that touches httpx/curl_cffi.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import date
from html import unescape
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel

from core.config import NICE_BASE, SNAPSHOT_DIR
from core.contracts import DocType
from core.provenance import SourceRecord, normalize_date, snapshot, snapshot_path

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# (status_code, content_bytes, content_type)
HttpGet = Callable[[str], tuple[int, bytes, str]]


def guidance_url(ta_id: str) -> str:
    """The recommendations CHAPTER, not the overview page. The overview is a shell;
    the date AND the rationale both live on /guidance/{ta}/chapter/1-recommendations."""
    return f"{NICE_BASE}/guidance/{ta_id.lower()}/chapter/1-recommendations"


# --------------------------------------------------------------------------- #
# Pure parsing
# --------------------------------------------------------------------------- #

_SCRIPT_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
# Closing block-level tags (and <br>) mark a line break in the extracted text.
_BLOCK_CLOSE = re.compile(
    r"</(p|div|section|article|li|ul|ol|h[1-6]|tr|table|dd|dt|dl|header|footer|nav|main)"
    r"\s*>|<br\s*/?>",
    re.IGNORECASE,
)
_SECTION = re.compile(r"\n\d+\s+[A-Z][a-z]")  # top-level NICE heading "2 Information…"
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_TIME_DT = re.compile(r'<time[^>]*\bdatetime="(\d{4}-\d{2}-\d{2})', re.IGNORECASE)
_PUBLISHED_TXT = re.compile(r"Published:?\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})")

# "1 Recommendation" is a prefix of both the singular ("1 Recommendation", as on
# recent TAs) and plural ("1 Recommendations") heading, so str.find matches either.
_RATIONALE_MARKERS = (
    "1 Recommendation",
    "Recommendation",
)
_WHY_MARKERS = (
    "Why the committee made these recommendations",
    "Why the committee made this recommendation",
)

# A withdrawn/replaced TA returns 200 with a notice and no recommendations. Detect it
# so the batch skips it gracefully (the spec) rather than failing loud on the (real)
# empty rationale.
_WITHDRAWN = re.compile(
    r"has been (withdrawn|updated and replaced)"
    r"|replaced by NICE technology appraisal"
    r"|this guidance has been replaced",
    re.IGNORECASE,
)


def is_withdrawn(html: str) -> bool:
    return bool(_WITHDRAWN.search(html_to_text(html)))


class ParsedGuidance(BaseModel):
    model_config = {"frozen": True}

    ta_id: str
    title: str
    published_date: date
    rationale_raw: str


def html_to_text(html: str) -> str:
    """HTML → text, one line per block element.

    Block-element boundaries (</p>, headings, list items, <br>, …) become newlines;
    everything else — including source line-wrapping INSIDE a paragraph — collapses to
    single spaces. This matters: a wrapped phrase like "indirect\\ncomparison" must read
    as "indirect comparison" or multi-word rubric cues silently fail to match during
    gold extraction. Block newlines are preserved so section-heading detection works."""
    html = _SCRIPT_STYLE.sub(" ", html)
    html = _BLOCK_CLOSE.sub("\x00", html)  # block boundaries → sentinel
    text = unescape(_TAG.sub(" ", html))  # inline tags → space
    text = re.sub(r"[^\S\x00]+", " ", text)  # all whitespace except sentinel → space
    text = text.replace("\x00", "\n")  # sentinel → newline
    return "\n".join(ln.strip() for ln in text.split("\n") if ln.strip())


def _extract_title(html: str, text: str) -> str:
    hm = _H1.search(html)
    if hm:
        t = unescape(_TAG.sub("", hm.group(1))).strip()
        if t:
            return t
    tm = _TITLE.search(html)
    if tm:
        return unescape(_TAG.sub("", tm.group(1))).split("|")[0].strip()
    return text.split("\n", 1)[0] if text else ""


def _extract_published(html: str, text: str) -> date | None:
    dm = _TIME_DT.search(html)
    if dm:
        d = normalize_date(dm.group(1))
        if d:
            return d
    pm = _PUBLISHED_TXT.search(text)
    return normalize_date(pm.group(1)) if pm else None


def _largest_section(text: str, marker: str, max_chars: int = 8000) -> str:
    """The longest block starting at any occurrence of `marker`, each truncated at the
    next top-level numbered heading.

    NICE chapter pages open with a table-of-contents that repeats every heading
    ("1 Recommendation\\n2 Information about…"), so the FIRST occurrence of the marker
    is a nav link whose section is a few characters. Taking the longest occurrence
    skips the nav and lands on the real content."""
    best = ""
    start = 0
    while True:
        i = text.find(marker, start)
        if i == -1:
            break
        tail = text[i : i + max_chars]
        nxt = _SECTION.search(tail, len(marker) + 1)  # skip the heading we started on
        block = (tail[: nxt.start()] if nxt else tail).strip()
        if len(block) > len(best):
            best = block
        start = i + len(marker)
    return best


def _extract_rationale(text: str) -> str:
    """The Recommendations section (carries the verdict + committee discussion). Falls
    back to the explicit 'Why the committee…' block if the section heading is absent.
    Blocks shorter than 60 chars are treated as nav fragments, not content."""
    for marker in _RATIONALE_MARKERS:
        block = _largest_section(text, marker)
        if len(block) >= 60:
            return block
    for marker in _WHY_MARKERS:
        block = _largest_section(text, marker)
        if len(block) >= 60:
            return block
    return ""


def parse_guidance(html: str, ta_id: str) -> ParsedGuidance:
    """Pure parse of a guidance page. Fails loud on missing date / empty rationale."""
    ta = ta_id.upper()
    text = html_to_text(html)
    published = _extract_published(html, text)
    if published is None:
        raise ValueError(
            f"{ta}: no published date found (no <time datetime> nor 'Published: …'); "
            "cannot stratify pre/post cutoff without it"
        )
    rationale = _extract_rationale(text)
    if not rationale.strip():
        raise ValueError(
            f"{ta}: empty rationale (no Recommendations / committee section found); "
            "nothing to mine for gold"
        )
    return ParsedGuidance(
        ta_id=ta,
        title=_extract_title(html, text),
        published_date=published,
        rationale_raw=rationale,
    )


# --------------------------------------------------------------------------- #
# Resilient fetch
# --------------------------------------------------------------------------- #


class GuidanceResult(BaseModel):
    model_config = {"frozen": True}

    ta_id: str
    status: Literal["ok", "unavailable"]
    parsed: ParsedGuidance | None = None
    source_record: SourceRecord | None = None
    reason: str = ""


def _looks_like_html(content: bytes, content_type: str) -> bool:
    if "html" in (content_type or "").lower():
        return True
    head = content[:1024].lower()
    return b"<html" in head or b"<!doctype html" in head


def _resilient_get(
    url: str, *, retries: int = 4, timeout: float = 30.0
) -> tuple[int, bytes, str]:
    """Default network path. browser UA + redirects; backoff on 429/5xx; escalate to
    curl_cffi (Chrome TLS impersonation) on 403. Never used in tests."""
    headers = {"User-Agent": BROWSER_UA, "Accept": "text/html"}
    last = 0
    with httpx.Client(
        timeout=timeout, follow_redirects=True, headers=headers
    ) as client:
        for attempt in range(retries):
            r = client.get(url)
            last = r.status_code
            if r.status_code == 403:
                return _curl_cffi_get(url, timeout=timeout)
            if r.status_code == 429 or 500 <= r.status_code < 600:
                time.sleep(2**attempt)
                continue
            return r.status_code, r.content, r.headers.get("content-type", "")
    return last, b"", ""


def _curl_cffi_get(url: str, *, timeout: float = 30.0) -> tuple[int, bytes, str]:
    from curl_cffi import requests as cffi

    r = cffi.get(url, impersonate="chrome", timeout=timeout)
    return r.status_code, r.content, r.headers.get("content-type", "")


class NICEGuidanceClient:
    """Fetch a TA's final guidance → GuidanceResult. Idempotent (cached) + resilient."""

    def __init__(
        self,
        *,
        http_get: HttpGet | None = None,
        snapshot_root: Path = SNAPSHOT_DIR,
        sleep_s: float = 1.0,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http_get = http_get or _resilient_get
        self._root = snapshot_root
        self._sleep_s = sleep_s
        self._sleeper = sleeper

    def _cache_path(self, ta: str) -> Path:
        return self._root / "nice_guidance" / f"{ta}.json"

    def _load_cache(self, ta: str) -> GuidanceResult | None:
        path = self._cache_path(ta)
        if not path.exists():
            return None
        result = GuidanceResult.model_validate_json(path.read_text())
        # An 'ok' pointer is only valid if its content snapshot still exists on disk.
        if result.status == "ok" and result.source_record is not None:
            sha = result.source_record.content_sha256
            if not snapshot_path(sha, self._root).exists():
                return None
        return result

    def _save_cache(self, result: GuidanceResult) -> None:
        path = self._cache_path(result.ta_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(result.model_dump_json(indent=2))

    def fetch(self, ta_id: str) -> GuidanceResult:
        ta = ta_id.upper()
        cached = self._load_cache(ta)
        if cached is not None:
            return cached  # cache hit: no network, no politeness sleep

        url = guidance_url(ta)
        status, content, ctype = self._http_get(url)

        if status == 404:
            return self._unavailable(ta, "404 not found / withdrawn or replaced")
        if status != 200:
            return self._unavailable(ta, f"http {status} after retries")
        if not _looks_like_html(content, ctype):
            return self._unavailable(ta, "non-HTML body (block page?)")

        html_str = content.decode("utf-8", "replace")
        # Withdrawn/replaced TAs return 200 with a notice and no rationale — skip them
        # gracefully BEFORE the fail-loud parse, so one dead TA never aborts the batch.
        if is_withdrawn(html_str):
            return self._unavailable(ta, "withdrawn or replaced by newer guidance")

        # Fail loud on a reachable-but-unparseable page (missing date / empty rationale).
        parsed = parse_guidance(html_str, ta)
        rec = snapshot(
            content,
            source="nice",
            source_id=ta,
            url=url,
            doc_date=parsed.published_date,
            doc_type=DocType.ta_final_guidance,  # gold-bearing → excluded from retrieval
            appraisal_id=ta,
            root=self._root,
        )
        self._sleeper(self._sleep_s)  # politeness, network path only
        result = GuidanceResult(
            ta_id=ta, status="ok", parsed=parsed, source_record=rec
        )
        self._save_cache(result)
        return result

    def _unavailable(self, ta: str, reason: str) -> GuidanceResult:
        result = GuidanceResult(ta_id=ta, status="unavailable", reason=reason)
        self._save_cache(result)  # cache the miss so withdrawn TAs aren't refetched
        return result
