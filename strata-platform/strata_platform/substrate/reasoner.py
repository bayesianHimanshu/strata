"""The model seam. Capabilities depend on the Reasoner protocol, never on a vendor.

AzureOpenAIReasoner targets an Azure OpenAI deployment of a GPT-5.x reasoning model:
temperature is unsupported (rejected by reasoning models), so we omit it and use
max_completion_tokens + reasoning_effort. Auth is api-key OR Entra managed identity
(token) — managed identity is the production path (no secrets in the app).
"""
from __future__ import annotations

from typing import Protocol

from strata_platform.config import Settings, get_settings


class Reasoner(Protocol):
    def complete(self, prompt: str, *, system: str | None = None) -> str: ...


class AzureOpenAIReasoner:
    def __init__(self, settings: Settings | None = None) -> None:
        self._s = settings or get_settings()
        if not self._s.azure_openai_endpoint:
            raise RuntimeError("AZURE_OPENAI_ENDPOINT not configured")

    def _headers(self) -> dict[str, str]:
        h = {"content-type": "application/json"}
        if self._s.azure_openai_api_key:
            h["api-key"] = self._s.azure_openai_api_key
        else:
            # Entra managed identity — no secret in the app
            from azure.identity import DefaultAzureCredential

            token = DefaultAzureCredential().get_token(
                "https://cognitiveservices.azure.com/.default"
            ).token
            h["Authorization"] = f"Bearer {token}"
        return h

    def complete(self, prompt: str, *, system: str | None = None,
                 max_retries: int = 6) -> str:
        import time

        import httpx

        url = (f"{self._s.azure_openai_endpoint}/openai/deployments/"
               f"{self._s.azure_openai_deployment}/chat/completions"
               f"?api-version={self._s.azure_openai_api_version}")
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body = {
            "messages": messages,
            "max_completion_tokens": self._s.azure_openai_max_completion_tokens,
            "reasoning_effort": self._s.azure_openai_reasoning_effort,
            # NOTE: no temperature — GPT-5.x reasoning models reject it.
        }
        for attempt in range(max_retries):
            r = httpx.post(url, headers=self._headers(), json=body, timeout=180.0)
            if r.status_code in (429, 503):     # TPM/RPM quota or transient — back off
                retry_after = r.headers.get("retry-after")
                wait = float(retry_after) if retry_after else min(2**attempt, 30)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"] or ""
        raise RuntimeError(
            f"Azure OpenAI: exhausted {max_retries} retries on 429/503 (TPM quota — "
            "raise the deployment capacity or reduce request size)")


class EchoReasoner:
    """Deterministic offline stand-in for tests / local boot without Azure."""

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        return "[]"


class KeywordReasoner:
    """Deterministic offline reasoner: emits the vulnerability categories whose
    pre-registered cues appear in the prompt. With the closed/open prompt skeleton this
    makes open-book (evidence-rich prompt) ground on real cues while closed-book (bare
    decision text) stays sparse — so the open/closed contrast runs with no Azure/network,
    for local boot and the demo. The production path uses AzureOpenAIReasoner (GPT-5.x)."""

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        import json

        from strata_platform.eval.rubric import CATEGORY_CUES

        low = prompt.lower()
        hits = [cat.value for cat, cues in CATEGORY_CUES.items()
                if any(cue in low for cue in cues)]
        return json.dumps(hits)


def get_reasoner() -> Reasoner:
    s = get_settings()
    if s.azure_openai_endpoint:
        return AzureOpenAIReasoner(s)
    if s.offline_reasoner == "keyword":
        return KeywordReasoner()
    return EchoReasoner()
