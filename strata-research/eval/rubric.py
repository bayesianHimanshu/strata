from __future__ import annotations

import hashlib
import json
from pathlib import Path

from core.contracts import VulnCategory

RUBRIC_VERSION = "1.0.0"

# How a predicted vulnerability is scored against gold. Frozen into the hash so the
# scoring rule cannot be redefined after results exist.
MATCH_RULE = "category-level, per decision: (decision_id, VulnCategory) set overlap"

# Cue lexicon per category. Lowercased substring cues; the gold extractor emits the
# verbatim committee sentence containing a cue. Deliberately high-precision phrasing
# drawn from NICE/G-BA committee language. 'other' has no cues (it is the residual).
CATEGORY_CUES: dict[VulnCategory, tuple[str, ...]] = {
    VulnCategory.comparator: (
        "comparator",
        "did not reflect",
        "not reflect nhs",
        "not reflect uk",
        "standard of care",
        "relevant comparison",
        "appropriate comparison",
    ),
    VulnCategory.surrogate_endpoint_immaturity: (
        "immature",
        "not yet mature",
        "surrogate",
        "progression-free survival",
        "overall survival data",
        "os data",
        "interim analysis",
        "follow-up was short",
    ),
    VulnCategory.missing_pro: (
        "patient-reported",
        "quality of life",
        "health-related quality of life",
        "hrqol",
        "eq-5d",
        "utility values",
    ),
    VulnCategory.icer_uncertainty: (
        "icer",
        "cost-effectiveness estimate",
        "incremental cost-effectiveness",
        "highly uncertain",
        "cost effectiveness was uncertain",
        "above the range",
    ),
    VulnCategory.generalizability: (
        "generalis",
        "generaliz",
        "uk clinical practice",
        "nhs clinical practice",
        "applicability",
        "trial population",
        "external validity",
    ),
    VulnCategory.trial_design_bias: (
        "single-arm",
        "single arm",
        "open-label",
        "crossover",
        "cross-over",
        "risk of bias",
        "post-hoc",
        "non-randomised",
        "naive comparison",
        "indirect comparison",
        "matching-adjusted",
    ),
    VulnCategory.budget_impact: (
        "budget impact",
        "affordab",
        "commercial arrangement",
        "patient access scheme",
    ),
    VulnCategory.other: (),
}

_LOCK_PATH = Path(__file__).with_name("rubric.lock")


def _canonical() -> str:
    """The deterministic serialization that the hash is taken over."""
    payload = {
        "rubric_version": RUBRIC_VERSION,
        "match_rule": MATCH_RULE,
        "categories": sorted(c.value for c in VulnCategory),
        "cues": {
            cat.value: sorted(cues) for cat, cues in CATEGORY_CUES.items()
        },
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=True)


def rubric_hash() -> str:
    """sha256 of the canonical rubric. Changes iff the spec changes."""
    return hashlib.sha256(_canonical().encode("utf-8")).hexdigest()


def commit_rubric() -> str:
    """Write the current rubric hash to eval/rubric.lock (the pre-registration act).
    Returns the committed hash. Call this deliberately, not from library code."""
    h = rubric_hash()
    _LOCK_PATH.write_text(f"{RUBRIC_VERSION}\n{h}\n")
    return h


def committed_hash() -> str | None:
    """The hash recorded in rubric.lock, or None if never committed."""
    if not _LOCK_PATH.exists():
        return None
    lines = _LOCK_PATH.read_text().splitlines()
    return lines[1].strip() if len(lines) >= 2 else None


def assert_rubric_committed() -> None:
    """Invariant #6 gate. Raise unless the live rubric matches the committed hash.

    The synthesizer calls this before running: a rubric edited after pre-registration
    (or never registered) blocks the run rather than silently rescoring.
    """
    recorded = committed_hash()
    if recorded is None:
        raise RuntimeError(
            "rubric not pre-registered: eval/rubric.lock is missing. Run "
            "`python -m eval.rubric --commit` before any synthesizer run."
        )
    live = rubric_hash()
    if live != recorded:
        raise RuntimeError(
            f"rubric drift: live hash {live[:12]}… != committed {recorded[:12]}…. "
            "Post-hoc edits are not allowed; bump RUBRIC_VERSION and re-commit."
        )


def _main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="STRATA rubric pre-registration")
    ap.add_argument("--commit", action="store_true", help="write eval/rubric.lock")
    ap.add_argument("--check", action="store_true", help="verify the committed hash")
    args = ap.parse_args()
    if args.commit:
        print(f"committed {RUBRIC_VERSION} {commit_rubric()}")
    elif args.check:
        assert_rubric_committed()
        print(f"rubric OK: {RUBRIC_VERSION} {rubric_hash()}")
    else:
        print(f"{RUBRIC_VERSION} {rubric_hash()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
