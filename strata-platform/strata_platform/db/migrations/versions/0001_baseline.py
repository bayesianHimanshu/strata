"""baseline: pgvector extension + all tables + HNSW ANN index on chunks.embedding

Revision ID: 0001_baseline
Revises:
Create Date: 2026-06-28
"""
from __future__ import annotations

from alembic import op

from strata_platform.db.models import Base

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # The vector column type requires the extension to exist first.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    Base.metadata.create_all(bind)
    # Approximate-nearest-neighbour index for cosine ranking (matches the <=> operator
    # used by PgVectorStore.search via cosine_distance).
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chunks_embedding_hnsw "
        "ON chunks USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
    Base.metadata.drop_all(bind)
