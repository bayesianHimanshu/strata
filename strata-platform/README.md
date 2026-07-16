# STRATA - Agentic IEG Platform

A platform for **Integrated Evidence Generation (IEG)** built on a validated *trust
substrate*: provenance on every claim, a temporal leakage boundary enforced in code,
a pre-registered evaluation harness, and interchangeable capability agents. The first
capability - **HTA Archaeology** (anticipating the evidence concerns an HTA committee
raises) - is the one validated in the STRATA experiments; three more are conforming
stubs on the same spine.

The substrate, the API, async job orchestration, the leakage boundary, the eval
harness, and HTA Archaeology run end to end. Built out from the foundation: typed
public-data source clients (CT.gov / PubMed / openFDA / NICE), molecule-scoped
leakage-filtered ingestion with structure-aware chunking, the **pgvector** retrieval
backend (boundary predicates compile to SQL; the four boundary tests pass against both
the in-memory and pgvector backends), DB-backed jobs + a digest-verified provenance
ledger, Alembic migrations (pgvector extension + HNSW index), the three roadmap
capabilities as real retrieval-backed agents, and hardening (Key Vault secret refs,
health probes, structured logging, rate limiting, CI).

## Architecture

```
  API (Container App)            Worker (Container App, queue-scaled)
        │                                   │
        ▼                                   ▼
  ┌──────────────────── capability agents ───────────────────┐
  │ hta_archaeology(✓) · endpoint_landscape · evidence_synth │
  │                    · safety_surveillance                  │
  └───────────────────────────┬──────────────────────────────┘
                              │
  ┌──────────────── trust substrate (shared services) ───────┐
  │ substrate/  contracts · provenance ledger · reasoner      │
  │             (Azure OpenAI) · RetrievalBoundary · store    │
  │ sources/    typed public-data clients                     │
  │ eval/       hashed rubric · metrics · kappa               │
  └───────────────────────────────────────────────────────────┘
   Postgres(pgvector) · Blob (snapshots) · Storage Queue (jobs) · Key Vault · Entra
```

The design rules carried from STRATA: pure Python with typed contracts, **no agent
frameworks**, deterministic orchestration, provenance by construction, leakage control
enforced as predicates (a leaky query is unrepresentable), and fail-loud.

## Run locally

```bash
cp .env.example .env                       # defaults work as-is (EchoReasoner, in-proc jobs)
pip install -e ".[dev]"
uvicorn strata_platform.api.main:app --reload
# open http://localhost:8000/docs
```

Or the full local stack (Postgres+pgvector + API) mirroring Azure:

```bash
docker compose up --build
```

Smoke it:

```bash
curl localhost:8000/health
curl localhost:8000/capabilities
curl -X POST localhost:8000/jobs -H 'content-type: application/json' -d '{
  "capability":"hta_archaeology",
  "decision":{"decision_id":"TA1156","agency":"NICE","decision_date":"2026-05-21",
              "drug":"osimertinib","indication":"EGFR-mutated NSCLC"},
  "params":{"mode":"closed_book"}}'
# -> {"job_id": "...", "status":"queued"}  then GET /jobs/{job_id}
```

Locally the model backend is an `EchoReasoner` stand-in (no Azure needed). Set
`AZURE_OPENAI_ENDPOINT` to use a real GPT-5.x deployment.

Tests (no Azure, no DB, no network):

```bash
pytest -q
```

## Deploy to Azure

Prereqs: `az login`, Terraform ≥ 1.6, a subscription with Azure OpenAI + the GPT-5.x
model available in your region.

```bash
cd infra/terraform
terraform init
export TF_VAR_subscription_id=<SUB_ID>

# Pass 1 - create the ACR (+ identity) so we have somewhere to push the image:
terraform apply -target=azurerm_container_registry.acr \
                -target=azurerm_user_assigned_identity.app \
                -target=azurerm_role_assignment.acr_pull
ACR=$(terraform output -raw acr_login_server)
az acr login -n ${ACR%%.*}
docker build -t $ACR/strata-platform:latest ../..        && docker push $ACR/strata-platform:latest
docker build -t $ACR/strata-frontend:latest ../../frontend && docker push $ACR/strata-frontend:latest

# Pass 2 - full apply with the images + API keys (stored in Key Vault):
terraform apply \
  -var container_image=$ACR/strata-platform:latest \
  -var frontend_image=$ACR/strata-frontend:latest \
  -var ncbi_api_key=$NCBI_API_KEY -var openfda_api_key=$OPENFDA_API_KEY

terraform output api_url frontend_url
```

Then migrate, seed gold, ingest a real corpus, and reproduce the finding (the API
container has DB + Azure OpenAI access via managed identity):

```bash
APP=strata-api; RG=strata-rg
az containerapp exec -n $APP -g $RG --command "python -m strata_platform.manage migrate"
az containerapp exec -n $APP -g $RG --command "python -m strata_platform.manage load-gold"
az containerapp exec -n $APP -g $RG --command "python -m strata_platform.manage ingest --limit 10"
az containerapp exec -n $APP -g $RG --command "python -m strata_platform.manage eval  --limit 10"
# eval prints per-category precision/recall for closed-book vs open-book + the deltas
```

What Terraform provisions: resource group, user-assigned **managed identity** (all
data-plane auth - no secrets in the app), ACR (Basic in the demo profile), Postgres
Flexible Server with **pgvector**, Storage (Blob snapshots + jobs Queue), Key Vault
(DB URL, connection strings, and API keys as secret refs), an **Azure OpenAI** account
with a **chat (GPT-5.x)** and an **embeddings** deployment, and three Container Apps
(API + queue-scaled worker + UI) with RBAC + health probes.

Key variables: `demo_profile` (default true - Basic ACR + worker scale-to-zero, ~$5-10/mo
idle), `openai_model` / `openai_model_version` (verify with `az cognitiveservices model
list -l <region>`; gpt-5.5 is `2026-04-24`), `model_cutoff` (keep aligned to the deployed
model - it defines the leakage-clean slice), `enable_easy_auth` (front the apps with
Entra Easy Auth; needs Graph app-registration rights), and `ncbi_api_key` / `openfda_api_key`.

`terraform destroy` when the showcase ends.

## Layout

```
strata_platform/
  config.py                 settings (env)
  substrate/                contracts · provenance · reasoner(AzureOpenAI) · embeddings ·
                            chunking · boundary · store(InMemory + PgVector)
  sources/                  CT.gov · PubMed · openFDA · NICE(index/guidance) · drug_identity(RxNorm)
  ingest/                   corpus build · structure-aware chunking · fail-loud health gate
  capabilities/             base · hta_archaeology(✓) · roadmap(×3) · registry
  jobs/                     runner · db_store · worker(Azure queue)
  eval/                     rubric(cues+lock) · harness(metrics·kappa·grounding·open-vs-closed) · hta_eval
  api/                      main · routes · auth(Entra) · observability(logging·ratelimit·otel)
  db/                       session(sync) · models(pgvector) · gold · ledger · migrations(alembic)
  data/                     SME gold + decisions
  manage.py                 migrate · load-gold · ingest · eval
infra/terraform/            Azure topology (chat+embeddings, KV secret refs, Easy Auth, demo profile)
tests/                      boundary(both backends) · sources · ingest · jobs · ledger · grounding · API
```

## The finding it reproduces

On leakage-clean HTA decisions, retrieval over public evidence converts an
over-confident parametric prior into a disciplined, higher-precision predictor: the
open-book **grounding gate** emits a predicted vulnerability category only when a
retrieved chunk (admitted under the boundary) supports it. `manage eval` runs closed-book
vs open-book over the SME gold and reports the per-category precision/recall and the
signed deltas - precision up under grounding, at some recall cost.

## Capabilities

- **HTA Archaeology** (validated) - classifier; closed vs open book, grounding-gate precision lift.
- **Evidence Synthesis** (grounded generator) - two-pass extract -> automated groundedness
  gate -> narrative composed only from retained claims; a structured brief + dossier prose
  where every sentence traces to a claim and every claim to a source. UI at `/synthesis`.
- **Endpoint & Comparator Landscape** (indication-centric) - structured-first: deterministic
  endpoint/comparator counts from the ClinicalTrials.gov v2 structured fields; the model only
  clusters variant names and flags surrogates (lexicon-first); NICE signals where known;
  grounded design implications. Every entry traces to its NCT ids. UI at `/landscape`.
- **Safety-Signal Surveillance** (ported from Project VIGIL) - disproportionality (PRR/ROR)
  over FAERS via **guarded text-to-SQL**: the question becomes a read-only SELECT against the
  `vw_signal_metrics` view, a deny-by-default `sql_guard` (sqlglot) validates it before it
  runs, and the exact SQL is shown. Grounded summary + "screening, not causal" caveats; every
  query append-only audited. UI at `/surveillance`.

## Real-time context

One boundary, two modes via factories - **backtest** (`as_of = decision_date`, dossier/siblings
excluded; the experiment) and **live** (`as_of = today`, no exclusions; the product). Connectors
fetch public sources live (CT.gov / PubMed / openFDA) and a **generic connector** ingests a
URL / pasted text / uploaded file (SSRF-allowlisted, default-closed). Ingestion dedups by
content hash, embeds once, upserts pgvector, and honours a freshness TTL; context-prep is an
async job with per-connector progress. The `ContextPanel` (on `/hta` and `/synthesis`) lets you
add a source and re-run to watch it appear in the provenance.

## Status

Built out and test-green (97 tests, ruff clean, no network/DB in tests) and **deployed live**:
substrate, typed source clients, ingestion + pgvector retrieval (boundary holds on both
backends), DB-backed jobs + digest-verified ledger, Alembic migrations, HTA Archaeology with
the grounding gate, Evidence Synthesis with the groundedness gate, the real-time context
subsystem, the two remaining roadmap capabilities, Easy Auth + Key Vault + health probes +
structured logging + rate limiting + CI, and the full Azure IaC.
