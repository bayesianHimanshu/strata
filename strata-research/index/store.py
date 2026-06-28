"""The vector store and its default chunking.

Two design commitments:

  1. `structure_aware_prefixed` chunking is the default (CLAUDE.md). Chunks respect
     document structure (headings / paragraph breaks) and each carries a breadcrumb
     prefix so a retrieved fragment still announces where it came from — which both
     improves retrieval and preserves provenance into the chunk.

  2. Retrieval is leakage-gated by construction. `VectorStore.search` *requires* a
     LeakageFilter; the store applies it as a hard pre-filter and re-asserts the
     bound on every returned hit. There is no way to query the corpus without
     declaring the decision you are retrieving for (invariant #2).

`InMemoryStore` is the pure, dependency-free reference backend used in tests and the
PoV. It scores lexically (token overlap) so the leakage and provenance behavior is
testable with no embedding model or network. `QdrantStore` is the production backend
behind a lazy import; it shares the exact same leakage gate.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from core.contracts import Claim, DocType, Span

# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Chunk:
    """A retrievable unit. `text` is what gets embedded (breadcrumb-prefixed);
    `span` indexes the *raw* source content so a Claim can be reconstructed.

    Carries `doc_type` + `appraisal_id` (Phase 2 Task 1) so the corpus-composition
    boundary can exclude an appraisal's own gold-bearing dossier chunks, plus `drug`
    (normalized molecule key) / `indication` / `decision_id` (Corpus rebuild) so
    retrieval is molecule-scoped per decision instead of a single undifferentiated
    blob."""

    source_id: str
    doc_date: date | None
    section_path: str
    text: str  # prefixed text actually indexed/embedded
    raw_text: str  # the underlying source substring
    span: Span  # offsets into the raw source document
    doc_type: DocType | None = None
    appraisal_id: str | None = None
    drug: str | None = None  # normalized molecule key (sources.drug_identity)
    indication: str | None = None
    decision_id: str | None = None  # which decision's gather produced this chunk


def chunk_to_dict(c: Chunk) -> dict:
    """JSON-serializable form of a Chunk (for persisting a built corpus)."""
    return {
        "source_id": c.source_id,
        "doc_date": c.doc_date.isoformat() if c.doc_date else None,
        "section_path": c.section_path,
        "text": c.text,
        "raw_text": c.raw_text,
        "span": [c.span.start, c.span.end],
        "doc_type": c.doc_type.value if c.doc_type else None,
        "appraisal_id": c.appraisal_id,
        "drug": c.drug,
        "indication": c.indication,
        "decision_id": c.decision_id,
    }


def chunk_from_dict(d: dict) -> Chunk:
    return Chunk(
        source_id=d["source_id"],
        doc_date=date.fromisoformat(d["doc_date"]) if d["doc_date"] else None,
        section_path=d["section_path"],
        text=d["text"],
        raw_text=d["raw_text"],
        span=Span(start=d["span"][0], end=d["span"][1]),
        doc_type=DocType(d["doc_type"]) if d["doc_type"] else None,
        appraisal_id=d["appraisal_id"],
        drug=d.get("drug"),
        indication=d.get("indication"),
        decision_id=d.get("decision_id"),
    )


_HEADING = re.compile(r"^\s{0,3}(#{1,6}\s+.+|[A-Z][A-Za-z0-9 ,/()-]{2,60}:)\s*$")


def _segment_sections(text: str) -> list[tuple[str, int, int]]:
    """Split into (heading, start, end) sections on markdown/`Label:` headings,
    falling back to a single anonymous section. Offsets index `text`."""
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
    indication: str | None = None,
    decision_id: str | None = None,
    max_chars: int = 1200,
    overlap: int = 150,
) -> list[Chunk]:
    """Chunk `text` honoring section structure, prefixing each chunk with a
    `[source_id › Title › Section]` breadcrumb. Windows long sections with overlap.
    Spans index the raw source so retrieval stays reconstructable. `doc_type`,
    `appraisal_id`, `drug`, `indication`, `decision_id` propagate to every chunk for
    the corpus-composition boundary and molecule scoping.
    """
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
            raw = body[off : off + max_chars]
            if not raw.strip():
                continue
            abs_start = sec_start + off
            chunks.append(
                Chunk(
                    source_id=source_id,
                    doc_date=doc_date,
                    section_path=heading,
                    text=prefix + raw,
                    raw_text=raw,
                    span=Span(start=abs_start, end=abs_start + len(raw)),
                    doc_type=doc_type,
                    appraisal_id=appraisal_id,
                    drug=drug,
                    indication=indication,
                    decision_id=decision_id,
                )
            )
            if off + max_chars >= len(body):
                break
    return chunks


def chunks_from_record(record, text: str, *, doc_title: str = "", **kw) -> list[Chunk]:
    """Chunk a document, inheriting source_id / doc_date / doc_type / appraisal_id from
    its SourceRecord so provenance and the boundary tags flow automatically. The text
    is the extracted/decoded content of the snapshot referenced by `record`.
    """
    return structure_aware_prefixed_chunks(
        text,
        source_id=record.source_id,
        doc_date=record.doc_date,
        doc_title=doc_title,
        doc_type=record.doc_type,
        appraisal_id=record.appraisal_id,
        **kw,
    )


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(s: str) -> list[str]:
    return _TOKEN.findall(s.lower())


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float

    def to_claim(self) -> Claim:
        """Build the invariant-#1 Claim for this retrieved chunk. Only callable for
        a dated chunk — a hit that passed the leakage gate always has a date."""
        if self.chunk.doc_date is None:
            raise ValueError("cannot build a Claim from an undated chunk")
        return Claim(
            text=self.chunk.raw_text,
            source_id=self.chunk.source_id,
            span=self.chunk.span,
            retrieval_score=self.score,
            doc_date=self.chunk.doc_date,
        )


class Boundary(Protocol):
    """The retrieval gate: a date-only LeakageFilter or a composite RetrievalBoundary.

    Both expose chunk-level `filter` + `assert_admits`, so the store treats them
    identically. Retrieval cannot be performed without one — a query that does not
    declare the decision it retrieves for is unrepresentable (invariants #2 + Task 1).
    """

    def filter(self, chunks: list[Chunk]) -> list[Chunk]: ...

    def assert_admits(self, chunk: Chunk) -> None: ...


class VectorStore(Protocol):
    """Every backend exposes the same boundary-gated surface."""

    def add(self, chunks: list[Chunk]) -> None: ...

    def search(self, query: str, *, boundary: Boundary, k: int = 8) -> list[Hit]:
        """Retrieve top-k chunks admissible under `boundary`. Not optional."""
        ...


@dataclass
class InMemoryStore:
    """Pure reference backend. Lexical (tf-idf-ish cosine) scoring, no deps."""

    chunks: list[Chunk] = field(default_factory=list)
    _df: Counter = field(default_factory=Counter)

    def add(self, chunks: list[Chunk]) -> None:
        for c in chunks:
            self.chunks.append(c)
            for tok in set(_tokens(c.raw_text)):
                self._df[tok] += 1

    def _idf(self, tok: str) -> float:
        n = len(self.chunks)
        return math.log(1 + n / (1 + self._df.get(tok, 0)))

    def _score(self, q_tokens: list[str], chunk: Chunk) -> float:
        c_counts = Counter(_tokens(chunk.raw_text))
        if not c_counts:
            return 0.0
        num = sum(c_counts[t] * self._idf(t) for t in q_tokens)
        norm = math.sqrt(sum(v * v for v in c_counts.values())) or 1.0
        return num / norm

    def search(self, query: str, *, boundary: Boundary, k: int = 8) -> list[Hit]:
        # Hard pre-filter: build the admissible corpus first (invariant #2 + Task 1).
        admissible = boundary.filter(self.chunks)
        q = _tokens(query)
        scored = [Hit(c, self._score(q, c)) for c in admissible]
        scored = [h for h in scored if h.score > 0.0]
        scored.sort(key=lambda h: h.score, reverse=True)
        top = scored[:k]
        # Defense in depth: nothing inadmissible may escape, even via a backend bug.
        for h in top:
            boundary.assert_admits(h.chunk)
        return top


def QdrantStore(*args, **kwargs):  # noqa: N802 - factory mimicking a class
    """Production backend. Imported lazily so the pure core never depends on Qdrant.

    Deferred to Phase L wiring: it must reuse `LeakageFilter` as a Qdrant payload
    pre-filter (a `doc_date` range condition) AND re-assert `leakage.assert_admits`
    on results, identically to InMemoryStore. Until then, fail loudly rather than
    silently bypass the gate.
    """
    raise NotImplementedError(
        "QdrantStore lands with Phase L deployment wiring; use InMemoryStore for the "
        "PoV. The leakage gate must be enforced identically when it is added."
    )
