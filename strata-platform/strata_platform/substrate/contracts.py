"""Typed contracts — the shared vocabulary of the substrate.

Pydantic v2 throughout. These types are the interface between the substrate and every
capability agent; nothing crosses a layer boundary except instances of these.
"""
from __future__ import annotations

import enum
import hashlib
from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Provenance & sources
# --------------------------------------------------------------------------- #

class DocType(str, enum.Enum):
    ta_final_guidance = "ta_final_guidance"
    ta_committee_papers = "ta_committee_papers"
    ta_erg_report = "ta_erg_report"
    ta_acd = "ta_acd"
    trial_registry = "trial_registry"
    literature = "literature"
    label = "label"
    faers = "faers"
    external = "external"        # user-supplied real-time context (URL / paste / upload)


class SourceRecord(BaseModel):
    """One retrieved/ingested document, content-addressed for audit (ALCOA+)."""
    source: str                                  # 'nice' | 'pubmed' | ...
    source_id: str
    url: str | None = None
    doc_type: DocType | None = None              # None for raw/index snapshots (e.g. the
                                                 # NICE cancer index xlsx); set on every
                                                 # ingested document
    appraisal_id: str | None = None              # set for dossier docs (boundary)
    drug: str | None = None                      # normalized molecule key
    indication: str | None = None
    doc_date: date | None = None
    content_sha256: str
    fetched_at: datetime = Field(default_factory=_utcnow)

    @staticmethod
    def hash_content(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()


class Chunk(BaseModel):
    chunk_id: str = Field(default_factory=lambda: uuid4().hex)
    text: str
    doc_type: DocType
    appraisal_id: str | None = None
    drug: str | None = None
    doc_date: date | None = None
    source_id: str
    embedding: list[float] | None = None


# --------------------------------------------------------------------------- #
# Decisions, gold, predictions
# --------------------------------------------------------------------------- #

class VulnCategory(str, enum.Enum):
    comparator = "comparator"
    icer_uncertainty = "icer_uncertainty"
    missing_pro = "missing_pro"
    surrogate_endpoint_immaturity = "surrogate_endpoint_immaturity"
    trial_design_bias = "trial_design_bias"
    generalizability = "generalizability"
    budget_impact = "budget_impact"
    other = "other"


class Decision(BaseModel):
    decision_id: str                             # e.g. TA1133
    agency: str = "NICE"
    decision_date: date
    drug: str
    indication: str
    outcome: str | None = None
    rationale_raw: str | None = None


class GoldItem(BaseModel):
    decision_id: str
    categories: set[VulnCategory]
    annotator: str = "candidate"                 # 'candidate' | 'sme' | 'adjudicated'


class Provenance(BaseModel):
    """Why a claim is asserted — the audit trail for one prediction."""
    chunk_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    note: str | None = None


class Vulnerability(BaseModel):
    category: VulnCategory
    grounded: bool                               # open-book (True) vs parametric (False)
    provenance: Provenance | None = None


# --------------------------------------------------------------------------- #
# Evidence synthesis (the generator capability — grounded brief + dossier prose)
# --------------------------------------------------------------------------- #

class EvidenceDimension(str, enum.Enum):
    efficacy = "efficacy"
    comparator = "comparator"
    safety = "safety"
    economic = "economic"
    generalizability = "generalizability"
    other = "other"


class SupportLevel(str, enum.Enum):
    supported = "supported"      # cited chunk entails the claim
    partial = "partial"          # partially supported — kept, flagged
    unsupported = "unsupported"  # filtered out of the brief/narrative


class EvidenceClaim(BaseModel):
    claim_id: str = Field(default_factory=lambda: uuid4().hex[:8])
    dimension: EvidenceDimension
    text: str
    provenance: Provenance                       # chunk_ids + source_ids (non-empty)
    support: SupportLevel = SupportLevel.supported


class SynthesisDimension(BaseModel):
    dimension: EvidenceDimension
    claims: list[EvidenceClaim] = Field(default_factory=list)


class NarrativeParagraph(BaseModel):
    text: str
    claim_ids: list[str]                         # the claims this paragraph draws on


class SynthesisResult(BaseModel):
    decision_id: str | None = None
    drug: str
    indication: str
    brief: list[SynthesisDimension] = Field(default_factory=list)
    narrative: list[NarrativeParagraph] = Field(default_factory=list)
    groundedness_score: float = 0.0              # retained / generated claims
    filtered_claims: list[EvidenceClaim] = Field(default_factory=list)  # audit trail
    boundary: dict = Field(default_factory=dict)
    retrieved_chunks: int = 0


# --------------------------------------------------------------------------- #
# Capabilities
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Endpoint & comparator landscape (indication-centric; structured-first)
# --------------------------------------------------------------------------- #

class OutcomeKind(str, enum.Enum):
    primary = "primary"
    secondary = "secondary"


class HTASignal(str, enum.Enum):
    accepted = "accepted"
    contested = "contested"
    rejected = "rejected"
    unknown = "unknown"


class EndpointEntry(BaseModel):
    entry_id: str = Field(default_factory=lambda: uuid4().hex[:8])
    canonical: str                               # e.g. "Progression-Free Survival"
    kind: OutcomeKind
    is_surrogate: bool                           # PFS/ORR/DFS/EFS/pCR/TTP True; OS/QoL False
    trial_count: int
    raw_variants: list[str]                      # registry strings this cluster covers
    provenance: Provenance                       # nct ids


class ComparatorEntry(BaseModel):
    entry_id: str = Field(default_factory=lambda: uuid4().hex[:8])
    canonical: str                               # e.g. "Platinum-based chemotherapy"
    comparator_class: str | None = None
    trial_count: int
    raw_variants: list[str]
    hta_signal: HTASignal = HTASignal.unknown    # from NICE, where known
    provenance: Provenance


class DesignImplication(BaseModel):
    text: str
    refs: list[str]                              # entry_ids / nct ids it rests on


class LandscapeResult(BaseModel):
    indication: str
    as_of: date
    trials_analysed: int
    endpoints: list[EndpointEntry] = Field(default_factory=list)
    comparators: list[ComparatorEntry] = Field(default_factory=list)
    implications: list[DesignImplication] = Field(default_factory=list)
    boundary: dict = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Safety-signal surveillance (ported from VIGIL — guarded text-to-SQL over FAERS)
# --------------------------------------------------------------------------- #

class SignalRow(BaseModel):
    """One disproportionality row from vw_signal_metrics. Lenient (rows come from SQL;
    tolerate partial selects)."""
    model_config = {"extra": "ignore"}
    event_pt: str
    scope_drug: str | None = None
    a: int = 0
    b: int = 0
    c: int = 0
    d: int = 0
    n_total: int = 0
    prr: float | None = None
    ror: float | None = None
    signal_flag: bool = False


class GeneratedSQL(BaseModel):
    model_config = {"extra": "forbid"}
    sql: str
    rationale: str | None = None


class SignalNarration(BaseModel):
    model_config = {"extra": "forbid"}
    summary: str
    caveats: list[str] = Field(default_factory=list)


class SignalOutput(BaseModel):
    model_config = {"extra": "forbid"}
    generated_sql: str
    results: list[SignalRow] = Field(default_factory=list)
    summary: str
    caveats: list[str] = Field(default_factory=list)
    audit: dict[str, Any] = Field(default_factory=dict)


class CapabilityRequest(BaseModel):
    capability: str                              # registry key
    tenant_id: str = "default"
    decision: Decision | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class CapabilityResult(BaseModel):
    capability: str
    tenant_id: str
    vulnerabilities: list[Vulnerability] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    produced_at: datetime = Field(default_factory=_utcnow)


# --------------------------------------------------------------------------- #
# Jobs (async orchestration)
# --------------------------------------------------------------------------- #

class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class Job(BaseModel):
    job_id: str = Field(default_factory=lambda: uuid4().hex)
    tenant_id: str = "default"
    capability: str
    status: JobStatus = JobStatus.queued
    request: CapabilityRequest
    result: CapabilityResult | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
