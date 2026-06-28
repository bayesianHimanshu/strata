"""Alembic environment. Uses the platform's sync DB URL and the ORM metadata as the
migration target. Online mode only (the platform always has a reachable DB at migrate
time)."""
from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from strata_platform.config import get_settings
from strata_platform.db.models import Base

target_metadata = Base.metadata


def run_migrations_online() -> None:
    engine = create_engine(get_settings().database_url_sync, future=True)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():  # pragma: no cover
    url = get_settings().database_url_sync
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    run_migrations_online()
