"""HTTP surface. Capabilities are invoked as async jobs: POST /jobs returns a job_id
immediately; GET /jobs/{id} polls. Locally execution is a background task; in Azure the
job_id is enqueued to the Storage Queue for the worker.
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from strata_platform.api.auth import Principal, get_principal
from strata_platform.capabilities.registry import get_capability, list_capabilities
from strata_platform.config import get_settings
from strata_platform.eval.harness import rubric_hash
from strata_platform.jobs.runner import get_job_store, run_job
from strata_platform.substrate.contracts import CapabilityRequest, Job

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "rubric_hash": rubric_hash(),
            "environment": get_settings().environment}


@router.get("/capabilities")
def capabilities() -> dict:
    return {"capabilities": list_capabilities()}


@router.post("/jobs", response_model=Job, status_code=202)
def submit_job(request: CapabilityRequest, background: BackgroundTasks,
               principal: Principal = Depends(get_principal)) -> Job:
    try:
        get_capability(request.capability)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    request.tenant_id = principal.tenant_id
    job = get_job_store().create(request)
    s = get_settings()
    if s.queue_connection_string:        # Azure: enqueue for the worker
        _enqueue(job.job_id)
    else:                                # local: run in-process
        background.add_task(run_job, job.job_id)
    return job


@router.get("/jobs/{job_id}", response_model=Job)
def get_job(job_id: str, principal: Principal = Depends(get_principal)) -> Job:
    job = get_job_store().get(job_id)
    if job is None or job.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=404, detail="job not found")
    return job


def _enqueue(job_id: str) -> None:  # pragma: no cover - requires Azure queue
    from azure.storage.queue import QueueClient

    s = get_settings()
    QueueClient.from_connection_string(
        s.queue_connection_string, s.queue_name).send_message(job_id)
