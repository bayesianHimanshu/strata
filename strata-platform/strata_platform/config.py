"""Central configuration. All settings come from environment / .env (12-factor).

Nothing here reaches out to a network or DB at import time, so the package imports
cleanly in tests and CI without any live Azure resources.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    # --- service ---
    environment: str = Field("local", description="local | dev | prod")
    log_level: str = "INFO"

    # --- persistence ---
    database_url: str = Field(
        "postgresql+asyncpg://strata:strata@localhost:5432/strata",
        description="async SQLAlchemy URL (asyncpg). pgvector lives in this DB.",
    )

    # --- object storage (content-addressed snapshots) ---
    blob_connection_string: str | None = None
    blob_container: str = "snapshots"
    local_blob_dir: str = "./_snapshots"   # used when blob_connection_string is unset

    # --- async jobs ---
    queue_connection_string: str | None = None   # Azure Storage Queue; None => in-proc
    queue_name: str = "strata-jobs"

    # --- backend selection (local defaults keep tests/boot free of a live DB) ---
    jobs_backend: str = "memory"                  # memory | db
    retrieval_backend: str = "memory"             # memory | pgvector

    @property
    def database_url_sync(self) -> str:
        """Sync SQLAlchemy URL (psycopg) derived from the async URL. The DB-backed job
        store, ledger, and pgvector retrieval run synchronously so the capability -> job
        call chain stays a single, auditable thread."""
        return self.database_url.replace("+asyncpg", "+psycopg")

    # --- model backend: Azure OpenAI ---
    azure_openai_endpoint: str | None = None      # https://<resource>.openai.azure.com
    azure_openai_deployment: str = "gpt-5.5"      # deployment NAME, not model id
    azure_openai_api_version: str = "2025-04-01-preview"
    azure_openai_api_key: str | None = None       # unset => use Entra managed identity
    azure_openai_reasoning_effort: str = "low"
    azure_openai_max_completion_tokens: int = 2048
    # Offline reasoner when no Azure endpoint is configured: echo (returns []) or
    # keyword (cue-based, for the no-Azure local demo / eval).
    offline_reasoner: str = "echo"               # echo | keyword

    # --- embeddings (Azure OpenAI) ---
    azure_openai_embeddings_deployment: str = "text-embedding-3-small"
    azure_openai_embeddings_dim: int = 1536

    # Optional cheaper/faster deployment for the evidence-synthesis groundedness gate
    # (one model call per claim). None => use the main chat deployment.
    synthesis_gate_deployment: str | None = None

    # --- public-source API keys (optional; raise rate limits). In Azure these come
    #     from Key Vault references; locally from .env. Keyless still works (backoff). ---
    ncbi_api_key: str | None = None               # PubMed E-utilities throughput
    openfda_api_key: str | None = None            # openFDA throughput
    rxnorm_enabled: bool = True                    # brand->INN via RxNav (network at ingest)

    # --- real-time context subsystem ---
    context_freshness_ttl_hours: int = 24         # skip re-fetch within this window
    # SSRF: server-side URL fetch is DEFAULT-CLOSED. Only hosts whose domain matches an
    # entry here (suffix match) AND resolve to a public IP are fetchable.
    context_url_allowlist: list[str] = []
    context_max_url_bytes: int = 5_000_000
    context_max_file_bytes: int = 10_000_000
    # Direct NICE cancer-recommendations xlsx asset URL (the storyblok host is reliable;
    # the HTML page 502s). Set to skip page scraping in the NICE live-horizon connector.
    context_nice_xlsx_url: str | None = None

    # --- model leakage boundary default ---
    model_cutoff: str = "2025-12-01"              # GPT-5.5 training cutoff
    retrieval_buffer_days: int = 90

    # --- auth (Entra / OIDC) ---
    auth_enabled: bool = False                    # local dev => off; prod => on
    entra_tenant_id: str | None = None
    entra_api_audience: str | None = None         # app registration / client id
    entra_issuer: str | None = None

    # --- frontend / CORS ---
    cors_origins: list[str] = ["*"]   # tighten to the frontend URL in prod


@lru_cache
def get_settings() -> Settings:
    return Settings()
