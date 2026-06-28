"""DecisionMiner — the gold path (Phase 2 Task 3).

Consumes a decision's `rationale_raw` (dossier-derived committee text) and emits
GoldItem candidates mapped to the VulnCategory taxonomy. These are *candidates*: gold
quality comes from dual blind annotation + adjudication, with Cohen's κ reported
(eval.metrics.cohen_kappa). DecisionMiner is one annotator-shaped producer; a second
(human, or an LLM-backed extractor) provides the other vector for κ.

Isolation invariant (#5): this module imports nothing from agents.synthesizer and
holds no shared mutable state with it. The only shared dependency is the read-only
pre-registered rubric (eval.rubric), which is a spec, not state.
"""
from __future__ import annotations

import re
from typing import Protocol

from core.contracts import Decision, GoldItem, VulnCategory
from eval.rubric import CATEGORY_CUES

# Sentence splitter: committee text is prose; split on sentence punctuation followed
# by whitespace. Kept simple and deterministic (no NLP dependency).
_SENT = re.compile(r"(?<=[.;:])\s+|\n+")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT.split(text or "") if s.strip()]


class Extractor(Protocol):
    """rationale text -> [(category, verbatim evidence sentence)]."""

    def extract(self, text: str) -> list[tuple[VulnCategory, str]]: ...


class LexiconExtractor:
    """Deterministic, offline cue-based extractor over the pre-registered lexicon.

    For each category, emits the first committee sentence containing a category cue as
    the verbatim evidence span. One candidate per category per decision (the committee
    raising a concern once is enough to ground the gold label). 'other' is never
    emitted by cue (it has no cues); it remains available for human adjudication.
    """

    def __init__(self, cues: dict[VulnCategory, tuple[str, ...]] = CATEGORY_CUES) -> None:
        self._cues = cues

    def extract(self, text: str) -> list[tuple[VulnCategory, str]]:
        sentences = split_sentences(text)
        out: list[tuple[VulnCategory, str]] = []
        for category, cues in self._cues.items():
            if not cues:
                continue
            for sent in sentences:
                low = sent.lower()
                if any(cue in low for cue in cues):
                    out.append((category, sent))
                    break
        return out


class DecisionMiner:
    """HTA committee rationale -> GoldItem[]. Gold path only."""

    def __init__(
        self, extractor: Extractor | None = None, *, annotator: str = "miner:lexicon"
    ) -> None:
        self.extractor = extractor or LexiconExtractor()
        self.annotator = annotator

    def mine(self, decision: Decision) -> list[GoldItem]:
        """Extract candidate GoldItems from one decision's rationale. Reads ONLY the
        decision's own dossier text — no retrieval, no synthesizer state."""
        return [
            GoldItem(
                decision_id=decision.decision_id,
                category=category,
                evidence_span=evidence,
                annotator=self.annotator,
            )
            for category, evidence in self.extractor.extract(decision.rationale_raw)
        ]

    def mine_all(self, decisions: list[Decision]) -> list[GoldItem]:
        gold: list[GoldItem] = []
        for d in decisions:
            gold.extend(self.mine(d))
        return gold
