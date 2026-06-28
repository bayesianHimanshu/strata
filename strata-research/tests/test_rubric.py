"""Invariant #6: the rubric is pre-registered, hashed, and drift-protected."""
from __future__ import annotations

import pytest

from eval import rubric


def test_committed_hash_matches_live() -> None:
    # eval/rubric.lock is committed in the repo; the live rubric must match it.
    assert rubric.committed_hash() == rubric.rubric_hash()
    rubric.assert_rubric_committed()  # must not raise


def test_hash_is_deterministic() -> None:
    assert rubric.rubric_hash() == rubric.rubric_hash()


def test_drift_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate a post-hoc edit: the live hash changes, the lock does not.
    monkeypatch.setattr(rubric, "MATCH_RULE", "something different, edited post-hoc")
    assert rubric.rubric_hash() != rubric.committed_hash()
    with pytest.raises(RuntimeError, match="rubric drift"):
        rubric.assert_rubric_committed()


def test_missing_registration_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rubric, "committed_hash", lambda: None)
    with pytest.raises(RuntimeError, match="not pre-registered"):
        rubric.assert_rubric_committed()
