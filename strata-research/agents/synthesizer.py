"""EvidenceGapSynthesizer — Arm A (Phase 2 tail / Phase 3 entry).

Runs the vulnerability synthesizer in two first-class modes (invariant #3):

  * open-book   — retrieves from the index under a RetrievalBoundary (date cutoff +
                  dossier/sibling exclusion) and emits GROUNDED Vulnerabilities, each
                  carrying a Claim with provenance (invariant #1);
  * closed-book — retrieval disabled; emits ungrounded Predictions from parametric
                  memory only. This is the control: open − closed = attributable signal.

The actual reasoning is delegated to an injectable `Reasoner`. The PoV ships a
deterministic `KeywordReasoner` (cue-based, no LLM, no network) so the pipeline,
the boundary enforcement, and the rubric gating are all testable offline; a real
LLM-backed reasoner is a drop-in replacement implementing the same Protocol.

Isolation (invariant #5): this module imports the read-only rubric (eval.rubric) but
NOTHING from agents.decision_miner. Gold path and prediction path do not share state.
Every run is gated by `assert_rubric_committed()` — the rubric must be pre-registered
and unchanged before a synthesizer run is permitted (invariant #6).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal, Protocol

from core.config import LEAKAGE_BUFFER
from core.contracts import Claim, Decision, Prediction, VulnCategory, Vulnerability
from eval.rubric import CATEGORY_CUES, assert_rubric_committed
from index.boundary import RetrievalBoundary
from index.store import VectorStore

Mode = Literal["open", "closed"]


# --------------------------------------------------------------------------- #
# Reasoner seam
# --------------------------------------------------------------------------- #


class Reasoner(Protocol):
    """The judgment step. open_book grounds every output in a retrieved Claim;
    closed_book sees no evidence and returns ungrounded Predictions."""

    def open_book(
        self, decision: Decision, claims: list[Claim]
    ) -> list[Vulnerability]: ...

    def closed_book(self, decision: Decision) -> list[Prediction]: ...


def _categories_in(text: str) -> list[VulnCategory]:
    low = (text or "").lower()
    return [
        cat
        for cat, cues in CATEGORY_CUES.items()
        if cues and any(cue in low for cue in cues)
    ]


def _confidence(score: float) -> float:
    """Squash a (non-negative) retrieval score into (0, 1)."""
    return score / (score + 1.0) if score > 0 else 0.0


class KeywordReasoner:
    """Deterministic, offline reference reasoner over the pre-registered cue lexicon.

    Open-book: classifies each retrieved Claim by cue, keeps the highest-scoring claim
    per category, and grounds one Vulnerability per category in it.
    Closed-book: classifies only the decision's own indication text (no evidence),
    yielding a deliberately weak parametric baseline.
    """

    def open_book(
        self, decision: Decision, claims: list[Claim]
    ) -> list[Vulnerability]:
        best: dict[VulnCategory, Claim] = {}
        for claim in claims:
            for cat in _categories_in(claim.text):
                cur = best.get(cat)
                if cur is None or cur.retrieval_score < claim.retrieval_score:
                    best[cat] = claim
        return [
            Vulnerability(
                category=cat,
                claim=claim,
                confidence=round(_confidence(claim.retrieval_score), 4),
            )
            for cat, claim in sorted(best.items())
        ]

    def closed_book(self, decision: Decision) -> list[Prediction]:
        cats = _categories_in(f"{decision.indication} {decision.drug or ''}")
        return [
            Prediction(
                category=cat,
                confidence=0.3,  # weak prior: no evidence was consulted
                rationale_text="parametric prior (no retrieval)",
            )
            for cat in sorted(set(cats))
        ]


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SynthesisResult:
    decision_id: str
    mode: Mode
    vulnerabilities: tuple[Vulnerability, ...] = ()
    predictions: tuple[Prediction, ...] = ()
    boundary_policy: dict | None = None
    n_claims_retrieved: int = 0

    def predicted_pairs(self) -> set[tuple[str, VulnCategory]]:
        """(decision_id, category) pairs for scoring against gold (eval.metrics)."""
        cats = [v.category for v in self.vulnerabilities]
        cats += [p.category for p in self.predictions]
        return {(self.decision_id, c) for c in cats}


def attributable_signal(
    open_result: SynthesisResult, closed_result: SynthesisResult
) -> set[tuple[str, VulnCategory]]:
    """open − closed: the categories the system found WITH evidence that it did not
    already produce from parametric memory. The headline of Arm A."""
    return open_result.predicted_pairs() - closed_result.predicted_pairs()


# --------------------------------------------------------------------------- #
# Synthesizer
# --------------------------------------------------------------------------- #


@dataclass
class EvidenceGapSynthesizer:
    store: VectorStore
    reasoner: Reasoner = field(default_factory=KeywordReasoner)
    exclude_siblings: bool = True  # registered research parameter (Task 1)
    k: int = 8

    def synthesize(
        self,
        decision: Decision,
        *,
        mode: Mode,
        sibling_appraisal_ids: tuple[str, ...] = (),
        buffer: timedelta = LEAKAGE_BUFFER,
        molecules: tuple[str, ...] = (),
    ) -> SynthesisResult:
        # Invariant #6: refuse to run unless the rubric is pre-registered + unchanged.
        assert_rubric_committed()

        if mode == "closed":
            # Control: the store is never touched. No retrieval, no provenance.
            preds = self.reasoner.closed_book(decision)
            return SynthesisResult(
                decision_id=decision.decision_id,
                mode="closed",
                predictions=tuple(preds),
            )

        if mode == "open":
            boundary = RetrievalBoundary.for_decision(
                decision,
                buffer=buffer,
                sibling_appraisal_ids=sibling_appraisal_ids,
                exclude_siblings=self.exclude_siblings,
                molecules=molecules,
            )
            claims = self._retrieve(decision, boundary)
            vulns = self.reasoner.open_book(decision, claims)
            return SynthesisResult(
                decision_id=decision.decision_id,
                mode="open",
                vulnerabilities=tuple(vulns),
                boundary_policy=boundary.policy(),
                n_claims_retrieved=len(claims),
            )

        raise ValueError(f"unknown mode {mode!r} (expected 'open' or 'closed')")

    def _retrieve(
        self, decision: Decision, boundary: RetrievalBoundary
    ) -> list[Claim]:
        """Pooled per-category retrieval under the boundary, deduped by source span.
        The boundary is the only thing that can admit a chunk — there is no retrieval
        path that bypasses it (invariant #2 + Task 1)."""
        pooled: dict[tuple[str, int, int], Claim] = {}
        for cues in CATEGORY_CUES.values():
            if not cues:
                continue
            query = " ".join(cues)
            for hit in self.store.search(query, boundary=boundary, k=self.k):
                claim = hit.to_claim()
                key = (claim.source_id, claim.span.start, claim.span.end)
                cur = pooled.get(key)
                if cur is None or cur.retrieval_score < claim.retrieval_score:
                    pooled[key] = claim
        return list(pooled.values())
