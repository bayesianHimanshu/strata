"""Real-time context API: prepare context (live fetch) as an async job with per-connector
progress, add user-supplied sources inline, and report indexed status.

    POST /context/ingest   {drug, indication, as_of, mode, connectors[]} -> {context_job_id}
    GET  /context/jobs/{id} -> {status, progress[], summary}
    POST /context/add       {kind:url|text|file, value, drug, indication, doc_date} -> ingested
    GET  /context/status    ?drug= -> {indexed_chunks, by_source, freshness}

The capability flow stays caller-orchestrated: ingest -> on complete -> run the capability
with params.mode/as_of. No capability needs to know about connectors.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from strata_platform.api.auth import Principal, get_principal
from strata_platform.context.connectors import (
    PUBLIC_CONNECTORS,
    ContextQuery,
    UrlNotAllowed,
)
from strata_platform.context.ingest import IngestionService, InMemoryFetchLedger
from strata_platform.substrate.embeddings import get_embedder
from strata_platform.substrate.store import InMemoryStore, get_store

router = APIRouter(prefix="/context")

# Shared, process-local state so the freshness ledger + dedup persist across requests
# (single-replica demo; the prod swap is a DB-backed ledger).
_LEDGER = InMemoryFetchLedger()
_SEEN: set[str] = set()
_CTX_JOBS: dict[str, dict] = {}


def _service(connector_keys: list[str]) -> IngestionService:
    from strata_platform.config import get_settings

    conns = {k: PUBLIC_CONNECTORS[k]() for k in connector_keys if k in PUBLIC_CONNECTORS}
    return IngestionService(get_store(), get_embedder(), conns,
                            freshness_ttl_hours=get_settings().context_freshness_ttl_hours,
                            ledger=_LEDGER, seen=_SEEN)


class IngestRequest(BaseModel):
    drug: str
    indication: str | None = None
    as_of: date | None = None
    mode: str = "live"
    connectors: list[str] = ["clinicaltrials", "pubmed", "openfda"]
    max_per_source: int = 25


class AddRequest(BaseModel):
    kind: str                      # url | text | file
    value: str
    drug: str
    indication: str | None = None
    doc_date: date | None = None
    filename: str | None = None


def _run_ingest(job_id: str, req: IngestRequest) -> None:
    job = _CTX_JOBS[job_id]
    job["status"] = "running"

    def progress(ev: dict) -> None:
        prog = job["progress"]
        for p in prog:
            if p["connector"] == ev["connector"]:
                p.update(ev)
                break
        else:
            prog.append(ev)

    try:
        q = ContextQuery(drug=req.drug, indication=req.indication,
                         as_of=req.as_of or date.today(),
                         max_per_source=req.max_per_source)
        summary = _service(req.connectors).ingest(q, connectors=req.connectors,
                                                  progress=progress)
        job["summary"] = summary.model_dump(mode="json")
        job["status"] = "succeeded"
    except Exception as exc:  # noqa: BLE001 - surface to the job record
        job["status"] = "failed"
        job["error"] = f"{type(exc).__name__}: {exc}"


@router.post("/ingest", status_code=202)
def ingest(req: IngestRequest, background: BackgroundTasks,
           principal: Principal = Depends(get_principal)) -> dict:
    job_id = uuid4().hex
    _CTX_JOBS[job_id] = {"context_job_id": job_id, "status": "queued",
                         "progress": [], "summary": None, "error": None}
    background.add_task(_run_ingest, job_id, req)
    return {"context_job_id": job_id}


@router.get("/jobs/{job_id}")
def get_context_job(job_id: str,
                    principal: Principal = Depends(get_principal)) -> dict:
    job = _CTX_JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="context job not found")
    return job


@router.post("/add")
def add(req: AddRequest, principal: Principal = Depends(get_principal)) -> dict:
    try:
        rec = _service([]).add_external(
            kind=req.kind, value=req.value, drug=req.drug, indication=req.indication,
            doc_date=req.doc_date or date.today(), filename=req.filename)
    except UrlNotAllowed as exc:
        raise HTTPException(status_code=400, detail=f"url rejected: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"source_id": rec.source_record.source_id,
            "doc_type": rec.source_record.doc_type.value if rec.source_record.doc_type else None,
            "chunks": len(rec.chunks),
            "content_sha256": rec.source_record.content_sha256,
            "drug": rec.source_record.drug}


@router.get("/status")
def status(drug: str, principal: Principal = Depends(get_principal)) -> dict:
    store = get_store()
    by_source: dict[str, int] = {}
    if isinstance(store, InMemoryStore):
        members = [c for c in store.chunks if c.drug and drug.lower() in c.drug.lower()]
        by_source = dict(Counter(c.doc_type.value for c in members))
        indexed = len(members)
    else:  # pgvector
        from sqlalchemy import func, select

        from strata_platform.db.models import ChunkRow
        from strata_platform.db.session import get_sessionmaker
        with get_sessionmaker()() as s:
            rows = s.execute(
                select(ChunkRow.doc_type, func.count()).where(
                    ChunkRow.drug.ilike(f"%{drug}%")).group_by(ChunkRow.doc_type)).all()
        by_source = {dt: n for dt, n in rows}
        indexed = sum(by_source.values())
    freshness = {k[2]: v[0].isoformat() for k, v in _LEDGER._t.items()  # noqa: SLF001
                 if k[0] == drug.lower()}
    return {"indexed_chunks": indexed, "by_source": by_source, "freshness": freshness}
