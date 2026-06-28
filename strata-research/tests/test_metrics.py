"""Scoring + inter-annotator agreement (eval.metrics)."""
from __future__ import annotations

from datetime import date

import pytest

from core.contracts import GoldItem, VulnCategory
from eval.metrics import (
    cohen_kappa,
    gold_pairs,
    kappa_over_items,
    score,
    score_stratified,
)

C = VulnCategory


def _gold(decision_id: str, cat: VulnCategory) -> GoldItem:
    return GoldItem(decision_id=decision_id, category=cat, evidence_span="x")


def test_score_basic_recall_precision() -> None:
    gold = {("TA1", C.comparator), ("TA1", C.icer_uncertainty), ("TA2", C.missing_pro)}
    pred = {("TA1", C.comparator), ("TA1", C.budget_impact)}  # 1 hit, 1 false pos
    sc = score(gold, pred)
    assert sc.tp == 1
    assert sc.fp == 1
    assert sc.fn == 2
    assert sc.precision == 0.5
    assert sc.recall == pytest.approx(1 / 3)
    assert sc.by_category[C.comparator] == (1, 0, 0)
    assert sc.by_category[C.budget_impact] == (0, 1, 0)


def test_score_stratified_splits_on_cutoff() -> None:
    cutoff = date(2026, 2, 1)
    gold = [
        _gold("TA_post", C.comparator),
        _gold("TA_pre", C.missing_pro),
    ]
    preds = [("TA_post", C.comparator), ("TA_pre", C.icer_uncertainty)]
    dates = {"TA_post": date(2026, 5, 1), "TA_pre": date(2025, 1, 1)}
    out = score_stratified(gold, preds, decision_date=dates, cutoff=cutoff)
    assert out["post_cutoff"].tp == 1 and out["post_cutoff"].fn == 0
    assert out["pre_cutoff"].tp == 0 and out["pre_cutoff"].fn == 1
    assert out["overall"].tp == 1


def test_undated_decision_treated_as_pre_cutoff() -> None:
    cutoff = date(2026, 2, 1)
    gold = [_gold("TA_x", C.comparator)]
    out = score_stratified(
        gold, [("TA_x", C.comparator)], decision_date={}, cutoff=cutoff
    )
    assert out["post_cutoff"].tp == 0  # cannot be claimed clean without a date
    assert out["pre_cutoff"].tp == 1


def test_cohen_kappa_perfect_and_chance() -> None:
    assert cohen_kappa([True, False, True], [True, False, True]) == 1.0
    # textbook-ish: partial agreement gives 0 < k < 1
    a = [True, True, False, False, True, False]
    b = [True, False, False, False, True, True]
    k = cohen_kappa(a, b)
    assert 0.0 < k < 1.0


def test_cohen_kappa_all_same_label_is_degenerate() -> None:
    # both annotators say True for everything -> pe == 1; perfect agreement -> 1.0
    assert cohen_kappa([True, True], [True, True]) == 1.0


def test_kappa_over_items_universe() -> None:
    universe = [
        ("TA1", C.comparator),
        ("TA1", C.icer_uncertainty),
        ("TA2", C.missing_pro),
    ]
    a = gold_pairs([_gold("TA1", C.comparator), _gold("TA1", C.icer_uncertainty)])
    b = gold_pairs([_gold("TA1", C.comparator), _gold("TA2", C.missing_pro)])
    k = kappa_over_items(universe, a, b)
    assert -1.0 <= k <= 1.0


def test_cohen_kappa_rejects_misaligned() -> None:
    with pytest.raises(ValueError):
        cohen_kappa([True], [True, False])
