"""Structured logging, request tracing, and a lightweight rate limiter.

JSON logs (one object per line) are what Container Apps / Log Analytics ingest cleanly.
Each request gets a correlation id and a timed access log. OpenTelemetry is wired lazily:
if the OTLP env + packages are present it exports spans; otherwise it is a no-op, so the
dependency stays optional and tests/local boot are unaffected.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from collections import defaultdict, deque
from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from strata_platform.config import get_settings


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if hasattr(record, "extra_fields"):
            payload.update(record.extra_fields)  # type: ignore[attr-defined]
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    root = logging.getLogger()
    if any(getattr(h, "_strata", False) for h in root.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    handler._strata = True  # type: ignore[attr-defined]
    root.handlers = [handler]
    root.setLevel(get_settings().log_level.upper())


_log = logging.getLogger("strata.access")


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Correlation id + timed access log per request (JSON)."""

    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid4().hex[:12]
        start = time.perf_counter()
        response = await call_next(request)
        dur_ms = round((time.perf_counter() - start) * 1000, 1)
        _log.info("request", extra={"extra_fields": {
            "request_id": rid, "method": request.method,
            "path": request.url.path, "status": response.status_code,
            "duration_ms": dur_ms}})
        response.headers["x-request-id"] = rid
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window in-memory rate limiter keyed by client (Easy Auth principal header if
    present, else source IP). Adequate for a single-replica demo / DoS backstop; a
    multi-replica production deployment would move this to a shared store (Redis)."""

    def __init__(self, app, *, limit: int = 120, window_s: int = 60) -> None:
        super().__init__(app)
        self._limit = limit
        self._window = window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _key(self, request: Request) -> str:
        principal = request.headers.get("x-ms-client-principal-name")
        if principal:
            return f"user:{principal}"
        client = request.client
        return f"ip:{client.host if client else 'unknown'}"

    async def dispatch(self, request: Request, call_next):
        if request.url.path in ("/health", "/"):
            return await call_next(request)
        now = time.time()
        q = self._hits[self._key(request)]
        while q and q[0] <= now - self._window:
            q.popleft()
        if len(q) >= self._limit:
            return JSONResponse(status_code=429,
                                content={"detail": "rate limit exceeded"})
        q.append(now)
        return await call_next(request)


def init_tracing(app) -> None:
    """Best-effort OpenTelemetry FastAPI instrumentation. No-op if the packages or the
    OTLP endpoint env are absent - so OTel stays an optional dependency."""
    import os

    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    try:  # pragma: no cover - optional dependency, exercised only when configured
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
        logging.getLogger("strata").info("opentelemetry instrumentation enabled")
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("strata").warning("otel init skipped: %s", exc)
