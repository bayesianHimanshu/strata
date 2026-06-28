"""Embed chunks and persist them to the pgvector-backed ``chunks`` table.

The Azure ingestion path: after ``assert_corpus_healthy`` passes, embed each chunk's text
through the Azure OpenAI embeddings deployment (respecting TPM quota via the embedder's
backoff) and upsert ``ChunkRow`` rows with the vector column. Local boot uses the
in-memory store instead, so this is only exercised in the Azure deployment / against a live
DB.
"""
from __future__ import annotations

from strata_platform.substrate.contracts import Chunk
from strata_platform.substrate.embeddings import Embedder, get_embedder


def embed_chunks(chunks: list[Chunk], embedder: Embedder | None = None) -> list[Chunk]:
    """Attach embeddings to chunks in place (returns the same list). Batches via the
    embedder so the Azure TPM-quota backoff applies."""
    embedder = embedder or get_embedder()
    vectors = embedder.embed([c.text for c in chunks])
    for c, v in zip(chunks, vectors):
        c.embedding = v
    return chunks


def persist_chunks(session_factory, chunks: list[Chunk], *,
                   embedder: Embedder | None = None,
                   replace_existing: bool = True) -> int:
    """Embed (if needed) + write chunks to the ChunkRow table (sync). Replaces any prior
    rows for the same source_ids so a re-ingest is idempotent. Returns rows written."""
    from sqlalchemy import delete

    from strata_platform.db.models import ChunkRow

    if any(c.embedding is None for c in chunks):
        embed_chunks(chunks, embedder)

    with session_factory() as session:
        if replace_existing and chunks:
            ids = sorted({c.source_id for c in chunks})
            session.execute(delete(ChunkRow).where(ChunkRow.source_id.in_(ids)))
        for c in chunks:
            session.add(ChunkRow(
                chunk_id=c.chunk_id, text=c.text, doc_type=c.doc_type.value,
                appraisal_id=c.appraisal_id, drug=c.drug, doc_date=c.doc_date,
                source_id=c.source_id, dim=len(c.embedding or []),
                embedding=c.embedding,
            ))
        session.commit()
    return len(chunks)
