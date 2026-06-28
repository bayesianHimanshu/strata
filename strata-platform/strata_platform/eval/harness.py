"""Evaluation substrate (shared service). Pre-registered, hashed rubric (eval.rubric) +
the metrics the STRATA experiment used: per-category precision/recall, inter-annotator
kappa, the grounding gate, and the open-vs-closed contrast that is the platform's headline
finding. Capabilities are evaluated through this, never ad hoc.
"""
from __future__ import annotations

from collections import Counter

from strata_platform.eval.rubric import (
    CATEGORY_CUES,
    assert_rubric_committed,
    committed_hash,
    rubric_hash,
)
from strata_platform.substrate.contracts import Chunk, VulnCategory

TAXONOMY = [c.value for c in VulnCategory]

__all__ = [
    "CATEGORY_CUES",
    "TAXONOMY",
    "assert_rubric_committed",
    "cohen_kappa",
    "committed_hash",
    "ground_category",
    "ground_categories",
    "open_vs_closed",
    "per_category",
    "rubric_hash",
]


def per_category(decision_ids, gold, pred) -> dict:
    out, hit_t, gold_t, pred_t = {}, 0, 0, 0
    for c in TAXONOMY:
        gp = sum(1 for d in decision_ids if c in gold.get(d, set()))
        pp = sum(1 for d in decision_ids if c in pred.get(d, set()))
        hit = sum(1 for d in decision_ids
                  if c in gold.get(d, set()) and c in pred.get(d, set()))
        out[c] = {"recall": (hit / gp) if gp else None,
                  "precision": (hit / pp) if pp else None,
                  "gold_positives": gp}
        hit_t, gold_t, pred_t = hit_t + hit, gold_t + gp, pred_t + pp
    return {"micro_recall": (hit_t / gold_t) if gold_t else None,
            "micro_precision": (hit_t / pred_t) if pred_t else None,
            "by_category": out}


def cohen_kappa(a: list[int], b: list[int]) -> float | None:
    n = len(a)
    if n == 0:
        return None
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum((ca[k] / n) * (cb[k] / n) for k in set(ca) | set(cb))
    return 1.0 if pe == 1 and po == 1 else (0.0 if pe == 1 else (po - pe) / (1 - pe))


# --------------------------------------------------------------------------- #
# Grounding gate — the precision mechanism (invariant #1 + the finding)
# --------------------------------------------------------------------------- #

def ground_category(category: VulnCategory, chunks: list[Chunk]) -> Chunk | None:
    """The retrieved chunk that best supports ``category``: the first chunk (highest
    retrieval rank, as passed) whose text contains one of the category's pre-registered
    cues. None → the prediction is NOT grounded and must be dropped."""
    cues = CATEGORY_CUES.get(category, ())
    for c in chunks:
        low = c.text.lower()
        if any(cue in low for cue in cues):
            return c
    return None


def ground_categories(predicted: set[VulnCategory],
                      chunks: list[Chunk]) -> dict[VulnCategory, Chunk]:
    """Keep only predicted categories a retrieved chunk supports; map each to its
    supporting chunk (its provenance). This is the gate by which retrieval raises
    precision over the prior-saturated closed book."""
    out: dict[VulnCategory, Chunk] = {}
    for cat in predicted:
        chunk = ground_category(cat, chunks)
        if chunk is not None:
            out[cat] = chunk
    return out


def open_vs_closed(decision_ids, gold, closed_pred, open_pred) -> dict:
    """The headline contrast: per-category precision/recall for closed-book (parametric)
    vs open-book (retrieval-grounded), plus the signed deltas. Reproduces the study's
    shape — precision up under grounding, at some recall cost."""
    closed = per_category(decision_ids, gold, closed_pred)
    open_ = per_category(decision_ids, gold, open_pred)
    return {
        "n_decisions": len(list(decision_ids)),
        "closed_book": closed,
        "open_book": open_,
        "delta": {
            "micro_precision": _sub(open_["micro_precision"], closed["micro_precision"]),
            "micro_recall": _sub(open_["micro_recall"], closed["micro_recall"]),
        },
        "rubric_hash": rubric_hash(),
    }


def _sub(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else round(a - b, 4)
