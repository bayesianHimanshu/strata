"""STRATA platform API. Run locally: uvicorn strata_platform.api.main:app --reload"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from strata_platform.api.context import router as context_router
from strata_platform.api.demo import router as demo_router
from strata_platform.api.observability import (
    RateLimitMiddleware,
    RequestLogMiddleware,
    configure_logging,
    init_tracing,
)
from strata_platform.api.routes import router
from strata_platform.config import get_settings

settings = get_settings()
configure_logging()

app = FastAPI(
    title="STRATA — Agentic IEG Platform",
    version="0.1.0",
    summary="Trust-substrate IEG platform: provenance, leakage boundary, capability agents.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestLogMiddleware)
app.add_middleware(RateLimitMiddleware)
app.include_router(router)
app.include_router(demo_router)
app.include_router(context_router)
init_tracing(app)


@app.get("/")
def root() -> dict:
    return {"service": "strata-platform", "env": settings.environment,
            "docs": "/docs", "capabilities": "/capabilities"}
