"""The embedding seam. Ingestion embeds chunks; pgvector ranks by cosine distance.

``AzureOpenAIEmbedder`` targets an Azure OpenAI embeddings deployment (e.g.
``text-embedding-3-small``, 1536-dim) — the production path; auth is api-key OR Entra
managed identity, identical to the reasoner. Batches with bounded backoff on 429 (TPM
quota). ``HashingEmbedder`` is a deterministic, dependency-free offline stand-in (same
dimensionality) so ingestion and the pgvector query path run locally and in tests with no
embedding service. Both implement the ``Embedder`` protocol.
"""
from __future__ import annotations

import hashlib
import math
import re
import time
from typing import Protocol

from strata_platform.config import Settings, get_settings

_TOKEN = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def embed_one(self, text: str) -> list[float]: ...


def _l2_normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class HashingEmbedder:
    """Deterministic hashing bag-of-words embedding, L2-normalized. Not semantic, but
    stable and offline — enough to exercise the ingestion + pgvector ranking path and to
    keep tests free of a live embedding service."""

    def __init__(self, dim: int = 1536) -> None:
        self.dim = dim

    def embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in _TOKEN.findall(text.lower()):
            h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(),
                               "big")
            idx = h % self.dim
            sign = 1.0 if (h >> 1) & 1 else -1.0
            vec[idx] += sign
        return _l2_normalize(vec)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_one(t) for t in texts]


class AzureOpenAIEmbedder:
    def __init__(self, settings: Settings | None = None) -> None:
        self._s = settings or get_settings()
        if not self._s.azure_openai_endpoint:
            raise RuntimeError("AZURE_OPENAI_ENDPOINT not configured")
        self.dim = self._s.azure_openai_embeddings_dim

    def _headers(self) -> dict[str, str]:
        h = {"content-type": "application/json"}
        if self._s.azure_openai_api_key:
            h["api-key"] = self._s.azure_openai_api_key
        else:
            from azure.identity import DefaultAzureCredential

            token = DefaultAzureCredential().get_token(
                "https://cognitiveservices.azure.com/.default"
            ).token
            h["Authorization"] = f"Bearer {token}"
        return h

    def embed(self, texts: list[str], *, batch_size: int = 64,
              max_retries: int = 6) -> list[list[float]]:
        import httpx

        url = (f"{self._s.azure_openai_endpoint}/openai/deployments/"
               f"{self._s.azure_openai_embeddings_deployment}/embeddings"
               f"?api-version={self._s.azure_openai_api_version}")
        out: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i: i + batch_size]
            for attempt in range(max_retries):
                r = httpx.post(url, headers=self._headers(),
                               json={"input": batch}, timeout=120.0)
                if r.status_code == 429:  # TPM quota — bounded backoff
                    time.sleep(min(2**attempt, 30))
                    continue
                r.raise_for_status()
                data = sorted(r.json()["data"], key=lambda d: d["index"])
                out.extend(d["embedding"] for d in data)
                break
            else:
                raise RuntimeError("Azure embeddings: exhausted retries on 429")
        return out

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


def get_embedder() -> Embedder:
    s = get_settings()
    if s.azure_openai_endpoint:
        return AzureOpenAIEmbedder(s)
    return HashingEmbedder(dim=s.azure_openai_embeddings_dim)
