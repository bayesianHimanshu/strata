"""Shared HTTP plumbing for source clients.

Deliberately thin: httpx only, explicit bounded backoff on 429 (and 5xx for hosts that
flap). No client hides control flow. CT.gov's Akamai TLS evasion (curl_cffi) and the
NICE WAF escalation live in their own modules.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from strata_platform.sources.endpoints import TIMEOUT_SECONDS, USER_AGENT


def build_client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(TIMEOUT_SECONDS),
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )


def get_json(
    client: httpx.Client,
    url: str,
    params: dict[str, Any] | None = None,
    retries: int = 4,
) -> dict[str, Any]:
    """GET JSON with bounded exponential backoff on 429. Raises on other errors."""
    r: httpx.Response | None = None
    for attempt in range(retries):
        r = client.get(url, params=params)
        if r.status_code == 429:
            time.sleep(2**attempt)
            continue
        r.raise_for_status()
        return r.json()
    assert r is not None
    r.raise_for_status()
    return r.json()
