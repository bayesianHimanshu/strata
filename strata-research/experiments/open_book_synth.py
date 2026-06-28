"""Open-book synthesizer adapter (Phase 2 Task 3.2).

`make_synthesizer()` returns the object `experiments/run_open_book.py` calls:
`predict_open_book(ProbeDecision, *, buffer_days) -> set[str]`.

CRITICAL (the spec's whole point): the reasoner is the SAME GPT-5.5 `OpenAIReasoner`
the closed-book run uses, and the prompt is the SAME closed-book system+user prompt
— open-book only ADDS the retrieved evidence as grounding context. So the open−closed
delta is attributable to retrieval alone, not to a different model or prompt.

Grounding = precision. A category GPT predicts is emitted ONLY if a retrieved chunk
(admitted under the RetrievalBoundary) supports it; otherwise it is dropped. That is
the mechanism by which retrieval can raise precision over the prior-saturated closed
book. Every emitted Vulnerability carries the supporting chunk as its Claim
(invariant #1).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from agents.synthesizer import EvidenceGapSynthesizer, _confidence
from core.contracts import Claim, Decision, Prediction, VulnCategory, Vulnerability
from eval.closed_book_probe import (
    CLOSED_BOOK_SYSTEM,
    ProbeDecision,
    build_closed_book_prompt,
    parse_categories,
)
from eval.rubric import CATEGORY_CUES
from experiments.build_retrieval_corpus import (
    DEFAULT_CORPUS,
    load_corpus,
    load_decisions,
)
from index.boundary import compute_sibling_map
from sources.drug_identity import normalize_drug

_TAXONOMY = [c.value for c in VulnCategory]
_EVIDENCE_CAP = 24  # chunks shown to the model, highest-retrieval-score first


class _OpenAILike:
    """Just the .complete surface we use from run_closed_book.OpenAIReasoner."""

    def complete(self, prompt: str, *, system: str | None = None) -> str: ...


def _probe_of(d: Decision) -> ProbeDecision:
    return ProbeDecision(
        decision_id=d.decision_id,
        decision_date=d.decision_date,
        agency=d.agency,
        drug=d.drug or "",
        indication=d.indication,
    )


def _evidence_block(claims: list[Claim]) -> str:
    top = sorted(claims, key=lambda c: c.retrieval_score, reverse=True)[:_EVIDENCE_CAP]
    lines = [f"[{c.source_id}] {c.text}" for c in top]
    return "\n".join(lines)


def _ground(category: VulnCategory, claims: list[Claim]) -> Claim | None:
    """The retrieved claim that best supports `category`: highest-scoring chunk whose
    text contains one of the category's pre-registered cues. None → not grounded."""
    cues = CATEGORY_CUES.get(category, ())
    matching = [c for c in claims if any(q in c.text.lower() for q in cues)]
    return max(matching, key=lambda c: c.retrieval_score) if matching else None


@dataclass
class OpenAIOpenBookReasoner:
    """Implements agents.synthesizer.Reasoner over the shared GPT-5.5 reasoner."""

    reasoner: _OpenAILike
    taxonomy: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.taxonomy = self.taxonomy or _TAXONOMY

    def _predict_categories(self, decision: Decision, evidence: str) -> set[str]:
        prompt = build_closed_book_prompt(_probe_of(decision), self.taxonomy)
        if evidence:
            prompt += (
                "\n\nRetrieved evidence (public sources predating the decision; cite "
                "only categories this evidence supports):\n" + evidence
            )
        return parse_categories(
            self.reasoner.complete(prompt, system=CLOSED_BOOK_SYSTEM), self.taxonomy
        )

    def open_book(self, decision: Decision, claims: list[Claim]) -> list[Vulnerability]:
        predicted = self._predict_categories(decision, _evidence_block(claims))
        vulns: list[Vulnerability] = []
        for value in sorted(predicted):
            category = VulnCategory(value)
            claim = _ground(category, claims)  # grounding gate = precision filter
            if claim is None:
                continue
            vulns.append(
                Vulnerability(
                    category=category,
                    claim=claim,
                    confidence=round(_confidence(claim.retrieval_score), 4),
                )
            )
        return vulns

    def closed_book(self, decision: Decision) -> list[Prediction]:
        # Provided for Reasoner-Protocol completeness; the closed-book RUN uses
        # eval.closed_book_probe directly. No retrieval, no provenance.
        predicted = self._predict_categories(decision, "")
        return [
            Prediction(category=VulnCategory(v), confidence=0.5)
            for v in sorted(predicted)
        ]


@dataclass
class OpenBookSynthesizer:
    """The object run_open_book.py drives. Wraps the populated store + GPT-5.5
    synthesizer; converts a ProbeDecision, builds the boundary, returns category set."""

    synth: EvidenceGapSynthesizer
    sibling_map: dict[str, frozenset[str]]

    def predict_open_book(self, decision: ProbeDecision, *, buffer_days: int) -> set[str]:
        core = Decision(
            agency=decision.agency,
            decision_id=decision.decision_id,
            decision_date=decision.decision_date,
            indication=decision.indication,
            outcome="",
            drug=decision.drug or None,
            appraisal_id=decision.decision_id,  # dossier-disjointness key
        )
        siblings = self.sibling_map.get(core.appraisal_id or "", frozenset())
        molecules = normalize_drug(core.drug or "").molecules
        result = self.synth.synthesize(
            core,
            mode="open",
            sibling_appraisal_ids=tuple(siblings),
            buffer=timedelta(days=buffer_days),
            molecules=tuple(molecules),
        )
        return {v.category.value for v in result.vulnerabilities}


def make_synthesizer(
    *,
    corpus_path=DEFAULT_CORPUS,
    decisions_path: str = "data/arm_a/decisions.json",
    reasoner: _OpenAILike | None = None,
    store=None,
    model: str = "gpt-5.5",
    max_tokens: int = 2048,
    reasoning_effort: str = "low",
) -> OpenBookSynthesizer:
    """Wire the populated store + GPT-5.5 reasoner + EvidenceGapSynthesizer.

    `reasoner`/`store` are injectable for offline tests; the default builds the real
    GPT-5.5 OpenAIReasoner (shared with the closed-book run) and loads the corpus
    persisted by build_retrieval_corpus.py."""
    if store is None:
        store = load_corpus(corpus_path)
    if reasoner is None:
        # Lazy import: only the real run needs the OpenAI client (and an API key).
        from eval.closed_book_probe import RunConfig
        from experiments.run_closed_book import GPT55_CUTOFF, OpenAIReasoner

        cfg = RunConfig(model=model, model_cutoff=GPT55_CUTOFF, max_tokens=max_tokens)
        reasoner = OpenAIReasoner(cfg, reasoning_effort=reasoning_effort)

    open_book_reasoner = (
        reasoner
        if isinstance(reasoner, OpenAIOpenBookReasoner)
        else OpenAIOpenBookReasoner(reasoner)
    )
    synth = EvidenceGapSynthesizer(store=store, reasoner=open_book_reasoner)
    sibling_map = compute_sibling_map(load_decisions(decisions_path))
    return OpenBookSynthesizer(synth=synth, sibling_map=sibling_map)
