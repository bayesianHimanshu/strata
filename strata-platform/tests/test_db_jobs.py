"""DB-backed job store round-trips Job ⇄ JobRow (offline, SQLite - only the jobs table)."""
from __future__ import annotations

from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from strata_platform.db.models import JobRow
from strata_platform.jobs.db_store import DbJobStore
from strata_platform.substrate.contracts import (
    CapabilityRequest,
    CapabilityResult,
    Decision,
    JobStatus,
    Vulnerability,
    VulnCategory,
)


def _store() -> DbJobStore:
    engine = create_engine("sqlite://")
    JobRow.__table__.create(engine)
    return DbJobStore(session_factory=sessionmaker(engine, expire_on_commit=False))


def _request() -> CapabilityRequest:
    return CapabilityRequest(
        capability="hta_archaeology", tenant_id="acme",
        decision=Decision(decision_id="TA1", decision_date=date(2026, 3, 1),
                          drug="osimertinib", indication="nsclc"),
        params={"mode": "open_book"},
    )


def test_create_and_get_roundtrip() -> None:
    store = _store()
    job = store.create(_request())
    got = store.get(job.job_id)
    assert got is not None
    assert got.capability == "hta_archaeology"
    assert got.tenant_id == "acme"
    assert got.request.decision.drug == "osimertinib"
    assert got.status == JobStatus.queued


def test_put_persists_result_and_status() -> None:
    store = _store()
    job = store.create(_request())
    job.status = JobStatus.succeeded
    job.result = CapabilityResult(
        capability="hta_archaeology", tenant_id="acme",
        vulnerabilities=[Vulnerability(category=VulnCategory.icer_uncertainty,
                                       grounded=True)],
    )
    store.put(job)
    got = store.get(job.job_id)
    assert got.status == JobStatus.succeeded
    assert got.result is not None
    assert got.result.vulnerabilities[0].category == VulnCategory.icer_uncertainty


def test_list_filters_by_tenant() -> None:
    store = _store()
    store.create(_request())
    other = _request()
    other.tenant_id = "other"
    store.create(other)
    assert {j.tenant_id for j in store.list("acme")} == {"acme"}
    assert len(store.list()) == 2
