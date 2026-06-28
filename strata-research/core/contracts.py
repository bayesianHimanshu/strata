"""Boundary contracts. Types are the spec (CLAUDE.md).

These Pydantic v2 models are frozen value objects that cross agent boundaries.
The orchestrator wires agents by passing these — an agent that needs anything not
expressible here is leaking hidden state and should be refactored.
"""
from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class VulnCategory(StrEnum):
    """The pre-registered evidence-vulnerability taxonomy (see eval/rubric.py).

    Membership is frozen at rubric-hash time (invariant #6); adding a category is a
    new rubric version, not an edit.
    """

    comparator = "comparator"
    surrogate_endpoint_immaturity = "surrogate_endpoint_immaturity"
    missing_pro = "missing_pro"
    icer_uncertainty = "icer_uncertainty"
    generalizability = "generalizability"
    trial_design_bias = "trial_design_bias"
    budget_impact = "budget_impact"
    other = "other"


class DocType(StrEnum):
    """What a document IS, for the corpus-composition boundary (Phase 2 Task 1).

    The dossier subset is gold-bearing: an appraisal's own committee papers, ERG/EAG
    report, ACD, and final guidance contain the committee's reasoning and must never
    reach the synthesizer for that same appraisal (they would hand it the answer).
    """

    ta_final_guidance = "ta_final_guidance"
    ta_committee_papers = "ta_committee_papers"
    ta_erg_report = "ta_erg_report"
    ta_acd = "ta_acd"
    trial_registry = "trial_registry"
    literature = "literature"
    label = "label"
    faers = "faers"


# The gold-bearing dossier doc_types. For a target decision D, a document carrying
# D's appraisal_id AND one of these types is excluded from D's retrieval corpus.
DOSSIER_DOC_TYPES: frozenset[DocType] = frozenset(
    {
        DocType.ta_final_guidance,
        DocType.ta_committee_papers,
        DocType.ta_erg_report,
        DocType.ta_acd,
    }
)


class Span(BaseModel):
    """Character offsets into a source document's frozen content.

    Offsets index the snapshotted bytes/text of `source_id`, so a span is
    re-derivable from frozen inputs alone.
    """

    model_config = {"frozen": True}

    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> Span:
        if self.end < self.start:
            raise ValueError(f"span end ({self.end}) precedes start ({self.start})")
        return self


class Claim(BaseModel):
    """A grounded textual claim. Invariant #1 lives in these fields: a claim that
    cannot name its source, span, retrieval score, and date is not a claim.

    `doc_date` is required (not optional as on SourceRecord): an emitted claim must
    be dateable, both for provenance and so it can be checked against the leakage
    bound it was retrieved under (invariant #2).
    """

    model_config = {"frozen": True}

    text: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    span: Span
    retrieval_score: float
    doc_date: date


class Vulnerability(BaseModel):
    """A predicted evidence-base vulnerability (Arm A OPEN-book synthesizer output).

    Carries a grounded Claim — invariant #1 holds by type: an open-book vulnerability
    that cannot point at a retrieved source span cannot be constructed.
    """

    model_config = {"frozen": True}

    category: VulnCategory
    claim: Claim
    confidence: float = Field(ge=0.0, le=1.0)


class Prediction(BaseModel):
    """A CLOSED-book (retrieval-disabled) prediction — the parametric-memory control
    (invariant #3). It carries NO provenance by construction: it is the model's prior,
    not a grounded claim, and exists only to be subtracted from the open-book result
    (open − closed = attributable signal). It must never be surfaced as user-facing
    system output; that channel is reserved for grounded Vulnerabilities (invariant #1).
    """

    model_config = {"frozen": True}

    category: VulnCategory
    confidence: float = Field(ge=0.0, le=1.0)
    rationale_text: str = ""  # the model's own words — explicitly ungrounded


class Decision(BaseModel):
    """An HTA committee decision. The prediction target's metadata and the raw
    rationale text that DecisionMiner turns into GoldItems (gold path only).
    """

    model_config = {"frozen": True}

    agency: str = Field(min_length=1)  # "NICE", "G-BA"
    decision_id: str = Field(min_length=1)  # TA number, G-BA procedure id
    decision_date: date
    indication: str
    outcome: str  # classified recommendation label (see sources.nice)
    rationale_raw: str = ""
    tumor_type: str | None = None  # for fairness/bias stratification (ELEVATE)
    drug: str | None = None  # for the same-drug sibling-appraisal policy (Task 1)
    # The appraisal this decision belongs to (its own TA id). Lets a RetrievalBoundary
    # be built straight from the target decision (see index.boundary).
    appraisal_id: str | None = None


class GoldItem(BaseModel):
    """One human-anchored gold label: this decision cited this vulnerability
    category, supported by this verbatim committee span.

    Produced only by the gold path (DecisionMiner). Carries the annotator so
    inter-annotator agreement (kappa) is computable.
    """

    model_config = {"frozen": True}

    decision_id: str = Field(min_length=1)
    category: VulnCategory
    evidence_span: str = Field(min_length=1)  # verbatim committee text
    annotator: str | None = None
