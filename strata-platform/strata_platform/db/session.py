"""Sync SQLAlchemy engine/session (psycopg). Lazy: the engine is created on first use, so
the package imports with no live database (local boot and tests don't touch Postgres).

The DB-backed paths (provenance ledger, jobs, pgvector retrieval) run synchronously, which
keeps the capability → job-runner → DB call chain a single coherent thread — easier to
audit than mixing async/await through the capability interface. The Azure deployment uses
this; local boot uses the in-memory stores.
"""
from __future__ import annotations

from functools import lru_cache

from strata_platform.config import get_settings


@lru_cache
def get_engine():
    from sqlalchemy import create_engine

    return create_engine(get_settings().database_url_sync, pool_pre_ping=True,
                         future=True)


@lru_cache
def get_sessionmaker():
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(get_engine(), expire_on_commit=False, future=True)


def init_models() -> None:  # pragma: no cover - requires live DB
    """Create tables (dev/test convenience). Production uses Alembic migrations, which
    also create the pgvector extension + the ANN index."""
    from strata_platform.db.models import Base

    Base.metadata.create_all(get_engine())
