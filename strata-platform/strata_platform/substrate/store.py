"""Retrieval store. ``search`` REQUIRES a RetrievalBoundary — there is no unbounded search
method, so leakage is structurally impossible at the call site.

Production backs onto pgvector (the same Postgres as the rest of the platform); the
in-memory backend keeps local boot and tests free of a live DB. The leakage boundary is
the SINGLE source of truth: ``boundary_sql_filter`` compiles the very same predicates the
in-memory ``boundary.admits`` enforces into SQLAlchemy expressions, and ``PgVectorStore``
re-asserts ``boundary.admits`` on every returned row (defense in depth). So both backends
admit/exclude identically — the four leakage-boundary tests pass against both.
"""
from __future__ import annotations

from sqlalchemy import and_, literal, not_, or_

from strata_platform.substrate.boundary import _DOSSIER, RetrievalBoundary
from strata_platform.substrate.contracts import Chunk, DocType


def _lexical_score(query: str, text: str) -> float:
    q = set(query.lower().split())
    t = set(text.lower().split())
    return len(q & t) / (len(q) or 1)


class InMemoryStore:
    """Local / test backend. Holds chunks in a list; boundary-filters then ranks."""

    def __init__(self) -> None:
        self._chunks: list[Chunk] = []

    @property
    def chunks(self) -> list[Chunk]:
        return self._chunks

    def add(self, chunks: list[Chunk]) -> None:
        self._chunks.extend(chunks)

    def upsert(self, chunks: list[Chunk], vectors: list[list[float]] | None = None
               ) -> int:
        """Idempotent insert keyed by content (source_id + text). Re-adding the same
        content is a no-op. Vectors are accepted for interface parity (in-memory ranks
        lexically). Returns the number of new chunks."""
        import hashlib

        existing = {hashlib.sha256(f"{c.source_id}\x00{c.text}".encode()).hexdigest()
                    for c in self._chunks}
        new = 0
        for i, c in enumerate(chunks):
            key = hashlib.sha256(f"{c.source_id}\x00{c.text}".encode()).hexdigest()
            if key in existing:
                continue
            if vectors is not None and i < len(vectors):
                c.embedding = vectors[i]
            self._chunks.append(c)
            existing.add(key)
            new += 1
        return new

    def search(self, query: str, boundary: RetrievalBoundary, *, k: int = 12
               ) -> list[Chunk]:
        eligible = [c for c in self._chunks if boundary.admits(c)]
        ranked = sorted(eligible, key=lambda c: _lexical_score(query, c.text),
                        reverse=True)
        return ranked[:k]

    def eligible_count(self, boundary: RetrievalBoundary) -> int:
        return sum(1 for c in self._chunks if boundary.admits(c))


def boundary_sql_filter(cols, boundary: RetrievalBoundary) -> list:
    """Compile a RetrievalBoundary to SQLAlchemy boolean expressions over ``cols`` (the
    chunks table / ORM class exposing .doc_date, .appraisal_id, .doc_type, .drug).

    This MUST mirror ``RetrievalBoundary.admits`` exactly — it is the same gate, expressed
    in SQL. ``cols.doc_type`` holds the DocType *value* (string).
    """
    clauses: list = []
    # 1. date: doc_date is not null AND (backtest: < cutoff; live: <= cutoff inclusive)
    clauses.append(cols.doc_date.is_not(None))
    if boundary.mode == "live":
        clauses.append(cols.doc_date <= boundary.cutoff)
    else:
        clauses.append(cols.doc_date < boundary.cutoff)
    # 2-3. dossier-disjointness + sibling policy apply only in backtest mode
    if boundary.mode == "backtest":
        dossier_vals = [d.value for d in _DOSSIER]
        clauses.append(
            not_(and_(cols.appraisal_id == boundary.decision_id,
                      cols.doc_type.in_(dossier_vals)))
        )
        if boundary.exclude_siblings and boundary.sibling_ids:
            clauses.append(
                or_(cols.appraisal_id.is_(None),
                    cols.appraisal_id.not_in(sorted(boundary.sibling_ids)))
            )
    # 4. drug scoping (only when chunk carries a drug): substring match, like admits()
    if boundary.molecules:
        drug_match = [
            or_(cols.drug == m, cols.drug.contains(m),
                literal(m).like("%" + cols.drug + "%"))
            for m in sorted(boundary.molecules)
        ]
        clauses.append(or_(cols.drug.is_(None), or_(*drug_match)))
    return clauses


class PgVectorStore:
    """Azure-deployed backend. Boundary predicates compile to a SQL WHERE; ranking is
    vector ANN over pgvector (``ORDER BY embedding <=> :q``). Runs against the sync DB
    session; ``boundary.admits`` is re-asserted on every hit (defense in depth)."""

    def __init__(self, session_factory=None, embedder=None) -> None:
        self._session_factory = session_factory
        self._embedder = embedder

    def _sessions(self):
        if self._session_factory is None:
            from strata_platform.db.session import get_sessionmaker
            self._session_factory = get_sessionmaker()
        return self._session_factory

    def add(self, chunks: list[Chunk]) -> None:
        """Embed + persist chunks to the pgvector table (uniform with InMemoryStore.add,
        so the seed/ingest paths work against either backend)."""
        from strata_platform.ingest.pgvector import persist_chunks

        if chunks:
            persist_chunks(self._sessions(), chunks, embedder=self._embedder)

    def upsert(self, chunks: list[Chunk], vectors: list[list[float]] | None = None
               ) -> int:
        """Idempotent insert keyed by a deterministic content id (source_id + text), so
        re-ingesting the same content is a no-op. Pre-computed vectors are written as-is;
        otherwise persist_chunks embeds."""
        import hashlib

        from sqlalchemy import select

        from strata_platform.db.models import ChunkRow

        if not chunks:
            return 0
        ids = []
        for i, c in enumerate(chunks):
            cid = hashlib.sha256(f"{c.source_id}\x00{c.text}".encode()).hexdigest()[:32]
            object.__setattr__(c, "chunk_id", cid)
            if vectors is not None and i < len(vectors):
                c.embedding = vectors[i]
            ids.append(cid)
        with self._sessions()() as session:
            present = set(session.execute(
                select(ChunkRow.chunk_id).where(ChunkRow.chunk_id.in_(ids))
            ).scalars().all())
        fresh = [c for c in chunks if c.chunk_id not in present]
        from strata_platform.ingest.pgvector import persist_chunks
        persist_chunks(self._sessions(), fresh, embedder=self._embedder,
                       replace_existing=False)
        return len(fresh)

    def search(self, query: str | list[float], boundary: RetrievalBoundary,
               *, k: int = 12) -> list[Chunk]:
        from sqlalchemy import select

        from strata_platform.db.models import ChunkRow

        if isinstance(query, str):
            if self._embedder is None:
                from strata_platform.substrate.embeddings import get_embedder
                self._embedder = get_embedder()
            q_emb = self._embedder.embed_one(query)
        else:
            q_emb = query

        stmt = (
            select(ChunkRow)
            .where(*boundary_sql_filter(ChunkRow, boundary))
            .order_by(ChunkRow.embedding.cosine_distance(q_emb))
            .limit(k)
        )
        with self._sessions()() as session:
            rows = session.execute(stmt).scalars().all()

        out: list[Chunk] = []
        for r in rows:
            c = Chunk(chunk_id=r.chunk_id, text=r.text, doc_type=DocType(r.doc_type),
                      appraisal_id=r.appraisal_id, drug=r.drug, doc_date=r.doc_date,
                      source_id=r.source_id)
            if not boundary.admits(c):  # defense in depth — nothing leaks past the gate
                raise RuntimeError(
                    f"PgVectorStore returned an inadmissible chunk {c.chunk_id} — "
                    "SQL filter and boundary.admits disagree (leakage gate breach)"
                )
            out.append(c)
        return out


_STORE: InMemoryStore | None = None


def get_store() -> InMemoryStore | PgVectorStore:
    """Retrieval backend. ``retrieval_backend=pgvector`` (Azure) returns the pgvector
    store; otherwise a process-singleton in-memory store (so seeded evidence is visible to
    the in-proc job runner). Both honor the same RetrievalBoundary."""
    from strata_platform.config import get_settings

    if get_settings().retrieval_backend == "pgvector":
        return PgVectorStore()
    global _STORE
    if _STORE is None:
        _STORE = InMemoryStore()
    return _STORE
