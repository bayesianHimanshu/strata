"""structure_aware_prefixed chunking — the platform default (CLAUDE.md / research).

Chunks respect document structure (headings / paragraph breaks) and each carries a
breadcrumb prefix ``[source_id › Title › Section]`` so a retrieved fragment still
announces where it came from — which both improves vector recall (the research found
~0.74 with this strategy) and preserves provenance into the chunk text. Long sections are
windowed with overlap.

Produces platform ``Chunk`` instances (the substrate contract); ``doc_type`` /
``appraisal_id`` / ``drug`` / ``doc_date`` / ``source_id`` propagate to every chunk so the
leakage boundary and molecule scoping apply at retrieval time. Pure; no I/O.
"""
from __future__ import annotations

import re
from datetime import date

from strata_platform.substrate.contracts import Chunk, DocType

_HEADING = re.compile(r"^\s{0,3}(#{1,6}\s+.+|[A-Z][A-Za-z0-9 ,/()-]{2,60}:)\s*$")


def _segment_sections(text: str) -> list[tuple[str, int, int]]:
    """Split into (heading, start, end) sections on markdown/`Label:` headings, falling
    back to a single anonymous section. Offsets index ``text``."""
    lines = text.splitlines(keepends=True)
    sections: list[tuple[str, int, int]] = []
    cur_heading = ""
    cur_start = 0
    pos = 0
    body_seen = False
    for ln in lines:
        if _HEADING.match(ln):
            if body_seen:
                sections.append((cur_heading, cur_start, pos))
            cur_heading = ln.strip().lstrip("#").strip().rstrip(":")
            cur_start = pos + len(ln)
            body_seen = False
        else:
            if ln.strip():
                body_seen = True
        pos += len(ln)
    if cur_start < len(text):
        sections.append((cur_heading, cur_start, len(text)))
    return sections or [("", 0, len(text))]


def structure_aware_prefixed_chunks(
    text: str,
    *,
    source_id: str,
    doc_date: date | None = None,
    doc_title: str = "",
    doc_type: DocType | None = None,
    appraisal_id: str | None = None,
    drug: str | None = None,
    max_chars: int = 1200,
    overlap: int = 150,
) -> list[Chunk]:
    """Chunk ``text`` honoring section structure, prefixing each chunk with a
    ``[source_id › Title › Section]`` breadcrumb. Windows long sections with overlap."""
    chunks: list[Chunk] = []
    crumb_root = " › ".join(p for p in (source_id, doc_title) if p)
    for heading, sec_start, sec_end in _segment_sections(text):
        crumb = " › ".join(p for p in (crumb_root, heading) if p)
        prefix = f"[{crumb}]\n" if crumb else ""
        body = text[sec_start:sec_end]
        if not body.strip():
            continue
        step = max(1, max_chars - overlap)
        for off in range(0, len(body), step):
            raw = body[off: off + max_chars]
            if not raw.strip():
                continue
            chunks.append(
                Chunk(
                    text=prefix + raw,
                    doc_type=doc_type or DocType.literature,
                    appraisal_id=appraisal_id,
                    drug=drug,
                    doc_date=doc_date,
                    source_id=source_id,
                )
            )
            if off + max_chars >= len(body):
                break
    return chunks
