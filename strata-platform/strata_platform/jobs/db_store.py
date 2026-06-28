"""DB-backed job store (the Azure async path).

The API (one container) writes a ``JobRow`` and enqueues the id; the worker (another
container) reads the row, runs the capability, and persists the result back. So the store
must be the shared DB, not process memory. Sync SQLAlchemy keeps it on the same thread as
the rest of the request path. Job ⇄ JobRow conversion round-trips the typed contracts as
JSON.
"""
from __future__ import annotations

from datetime import datetime, timezone

from strata_platform.substrate.contracts import CapabilityRequest, CapabilityResult, Job


def _to_row_kwargs(job: Job) -> dict:
    return {
        "job_id": job.job_id,
        "tenant_id": job.tenant_id,
        "capability": job.capability,
        "status": job.status.value,
        "request": job.request.model_dump(mode="json"),
        "result": job.result.model_dump(mode="json") if job.result else None,
        "error": job.error,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def _from_row(row) -> Job:
    return Job(
        job_id=row.job_id, tenant_id=row.tenant_id, capability=row.capability,
        status=row.status, request=CapabilityRequest.model_validate(row.request),
        result=CapabilityResult.model_validate(row.result) if row.result else None,
        error=row.error, created_at=row.created_at, updated_at=row.updated_at,
    )


class DbJobStore:
    def __init__(self, session_factory=None) -> None:
        self._sf = session_factory

    def _sessions(self):
        if self._sf is None:
            from strata_platform.db.session import get_sessionmaker
            self._sf = get_sessionmaker()
        return self._sf

    def create(self, request: CapabilityRequest) -> Job:
        from strata_platform.db.models import JobRow

        job = Job(capability=request.capability, tenant_id=request.tenant_id,
                  request=request)
        with self._sessions()() as s:
            s.add(JobRow(**_to_row_kwargs(job)))
            s.commit()
        return job

    def get(self, job_id: str) -> Job | None:
        from strata_platform.db.models import JobRow

        with self._sessions()() as s:
            row = s.get(JobRow, job_id)
            return _from_row(row) if row is not None else None

    def put(self, job: Job) -> None:
        from strata_platform.db.models import JobRow

        job.updated_at = datetime.now(timezone.utc)
        with self._sessions()() as s:
            row = s.get(JobRow, job.job_id)
            if row is None:
                s.add(JobRow(**_to_row_kwargs(job)))
            else:
                for k, v in _to_row_kwargs(job).items():
                    setattr(row, k, v)
            s.commit()

    def list(self, tenant_id: str | None = None) -> list[Job]:
        from sqlalchemy import select

        from strata_platform.db.models import JobRow

        stmt = select(JobRow)
        if tenant_id is not None:
            stmt = stmt.where(JobRow.tenant_id == tenant_id)
        with self._sessions()() as s:
            return [_from_row(r) for r in s.execute(stmt).scalars().all()]
