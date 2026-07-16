"""Invariant #2 - the experiment dies if this regresses.

For decision D: admit doc iff doc_date is known AND doc_date < D.decision_date - buffer.
Strict `<`; undated rejected; buffer respected.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pytest

from index.leakage import LeakageError, LeakageFilter


@dataclass
class _Doc:
    doc_date: date | None


def _filter(buffer_days: int = 90) -> LeakageFilter:
    return LeakageFilter(
        decision_date=date(2026, 6, 1), buffer=timedelta(days=buffer_days)
    )


def test_cutoff_is_decision_minus_buffer() -> None:
    assert _filter(90).cutoff == date(2026, 3, 3)


def test_admits_strictly_before_cutoff() -> None:
    f = _filter(90)  # cutoff 2026-03-03
    assert f.admits(date(2026, 3, 2)) is True
    assert f.admits(date(2020, 1, 1)) is True


def test_rejects_on_or_after_cutoff_strict() -> None:
    f = _filter(90)  # cutoff 2026-03-03
    assert f.admits(date(2026, 3, 3)) is False  # exactly at cutoff -> rejected
    assert f.admits(date(2026, 5, 1)) is False
    assert f.admits(date(2026, 6, 1)) is False  # the decision date itself


def test_undated_is_rejected() -> None:
    # Cannot prove it predates the decision -> must not be admitted.
    assert _filter().admits(None) is False


def test_buffer_widens_exclusion() -> None:
    near = date(2026, 4, 1)
    assert _filter(0).admits(near) is True  # cutoff = decision date
    assert _filter(90).admits(near) is False  # pushed before cutoff


def test_filter_drops_inadmissible_and_keeps_rest() -> None:
    f = _filter(90)
    docs = [
        _Doc(date(2026, 1, 1)),  # keep
        _Doc(date(2026, 3, 3)),  # drop (at cutoff)
        _Doc(None),  # drop (undated)
        _Doc(date(2025, 12, 31)),  # keep
    ]
    kept = f.filter(docs)
    assert [d.doc_date for d in kept] == [date(2026, 1, 1), date(2025, 12, 31)]


def test_assert_admits_raises_on_violation() -> None:
    f = _filter(90)
    f.assert_admits(_Doc(date(2025, 1, 1)))  # no raise
    with pytest.raises(LeakageError):
        f.assert_admits(_Doc(date(2026, 5, 1)))
    with pytest.raises(LeakageError):
        f.assert_admits(_Doc(None))
