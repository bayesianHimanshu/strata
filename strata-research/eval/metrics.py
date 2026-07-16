from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from core.contracts import GoldItem, VulnCategory

Pair = tuple[str, VulnCategory]  # (decision_id, category)


def gold_pairs(gold: Iterable[GoldItem]) -> set[Pair]:
    return {(g.decision_id, g.category) for g in gold}


@dataclass(frozen=True)
class ScoreCard:
    tp: int
    fp: int
    fn: int
    by_category: dict[VulnCategory, tuple[int, int, int]] = field(default_factory=dict)

    @property
    def recall(self) -> float:
        denom = self.tp + self.fn
        return self.tp / denom if denom else 0.0

    @property
    def precision(self) -> float:
        denom = self.tp + self.fp
        return self.tp / denom if denom else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def as_dict(self) -> dict:
        return {
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "recall": round(self.recall, 4),
            "precision": round(self.precision, 4),
            "f1": round(self.f1, 4),
            "by_category": {
                c.value: {"tp": t, "fp": f, "fn": n}
                for c, (t, f, n) in sorted(self.by_category.items())
            },
        }


def score(gold: set[Pair], predicted: set[Pair]) -> ScoreCard:
    """Recall/precision of predicted (decision_id, category) pairs against gold."""
    tp_pairs = gold & predicted
    fp_pairs = predicted - gold
    fn_pairs = gold - predicted
    by_cat: dict[VulnCategory, tuple[int, int, int]] = {}
    cats = {c for _, c in (gold | predicted)}
    for c in cats:
        t = sum(1 for _, cc in tp_pairs if cc == c)
        f = sum(1 for _, cc in fp_pairs if cc == c)
        n = sum(1 for _, cc in fn_pairs if cc == c)
        by_cat[c] = (t, f, n)
    return ScoreCard(
        tp=len(tp_pairs), fp=len(fp_pairs), fn=len(fn_pairs), by_category=by_cat
    )


def score_stratified(
    gold: Iterable[GoldItem],
    predicted: Iterable[Pair],
    *,
    decision_date: dict[str, date],
    cutoff: date,
) -> dict[str, ScoreCard]:
    """Score overall and split pre/post model cutoff (invariant #4). Post-cutoff =
    decision_date strictly after the cutoff - the leakage-clean slice.

    A decision_id absent from `decision_date` is conservatively treated as pre-cutoff
    (it cannot be claimed as clean test data without a date).
    """
    g_all = gold_pairs(gold)
    p_all = set(predicted)

    def is_post(decision_id: str) -> bool:
        d = decision_date.get(decision_id)
        return d is not None and d > cutoff

    g_post = {p for p in g_all if is_post(p[0])}
    p_post = {p for p in p_all if is_post(p[0])}
    return {
        "overall": score(g_all, p_all),
        "pre_cutoff": score(g_all - g_post, p_all - p_post),
        "post_cutoff": score(g_post, p_post),
    }


def cohen_kappa(a: list[bool], b: list[bool]) -> float:
    """Cohen's κ for two annotators' aligned binary judgments.

    κ = (po - pe) / (1 - pe). Returns 1.0 for perfect agreement when chance agreement
    is degenerate (pe == 1), else 0.0 for that degenerate case with disagreement.
    """
    if len(a) != len(b):
        raise ValueError("annotation vectors must be aligned (equal length)")
    n = len(a)
    if n == 0:
        raise ValueError("no items to score")
    po = sum(1 for x, y in zip(a, b, strict=True) if x == y) / n
    pa = sum(a) / n
    pb = sum(b) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    if pe == 1.0:
        return 1.0 if po == 1.0 else 0.0
    return (po - pe) / (1 - pe)


def kappa_over_items(
    items: Iterable[Pair],
    set_a: set[Pair],
    set_b: set[Pair],
) -> float:
    """Cohen's κ over a fixed universe of candidate (decision_id, category) items,
    where each annotator's label is presence in their gold set. The universe must be
    the agreed candidate space (e.g. all categories considered for each decision)."""
    universe = list(items)
    va = [it in set_a for it in universe]
    vb = [it in set_b for it in universe]
    return cohen_kappa(va, vb)
