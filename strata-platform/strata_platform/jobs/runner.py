"""Async job orchestration. A capability run is long (fetch + retrieve + LLM), so the
API submits a Job and returns immediately; execution happens out-of-band. Locally that
is a FastAPI background task over an in-memory store; in Azure it is a Storage-Queue
worker over the DB (see jobs.worker, db.models).
"""
from __future__ import annotations

from datetime import datetime, timezone

from strata_platform.capabilities.registry import get_capability
from strata_platform.substrate.contracts import CapabilityRequest, Job, JobStatus
from strata_platform.substrate.reasoner import get_reasoner
from strata_platform.substrate.store import get_store


class InMemoryJobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def create(self, request: CapabilityRequest) -> Job:
        job = Job(capability=request.capability, tenant_id=request.tenant_id,
                  request=request)
        self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def put(self, job: Job) -> None:
        job.updated_at = datetime.now(timezone.utc)
        self._jobs[job.job_id] = job

    def list(self, tenant_id: str | None = None) -> list[Job]:
        return [j for j in self._jobs.values()
                if tenant_id is None or j.tenant_id == tenant_id]


_STORE = InMemoryJobStore()


def get_job_store():
    """Job store backend. ``jobs_backend=db`` (Azure) returns the shared DB store so the
    API and the worker see the same jobs; otherwise the in-proc in-memory store."""
    from strata_platform.config import get_settings

    if get_settings().jobs_backend == "db":
        from strata_platform.jobs.db_store import DbJobStore

        return DbJobStore()
    return _STORE


def run_job(job_id: str, store: InMemoryJobStore | None = None) -> None:
    """Execute a queued job: dispatch to the capability over the shared substrate."""
    js = store or get_job_store()
    job = js.get(job_id)
    if job is None:
        return
    job.status = JobStatus.running
    js.put(job)
    try:
        capability = get_capability(job.request.capability)
        result = capability.run(job.request,
                                reasoner=get_reasoner(),
                                store=get_store())
        job.result = result
        job.status = JobStatus.succeeded
    except Exception as exc:  # noqa: BLE001 - surface to the job record
        job.status = JobStatus.failed
        job.error = f"{type(exc).__name__}: {exc}"
    js.put(job)
