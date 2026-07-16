"""The pre-registered scoring rubric (invariant #6).

Frozen here before any scored run: the taxonomy (VulnCategory), CATEGORY_CUES (the cue
lexicon the grounding gate keys on), and the MATCH_RULE. The whole thing is content-hashed
(``rubric_hash``) and the hash committed to ``rubric.lock``. ``assert_rubric_committed()``
refuses to proceed if the live rubric has drifted from the committed hash - a post-hoc edit
cannot silently change scoring. A real change is a new RUBRIC_VERSION with a freshly
committed hash, never an edit in place.

Import-only constants + pure functions; shared as a spec, not mutable state.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from strata_platform.substrate.contracts import VulnCategory

RUBRIC_VERSION = "1.0.0"
MATCH_RULE = "category-level, per decision: (decision_id, VulnCategory) set overlap"

# Cue lexicon per category. Lowercased substring cues drawn from NICE/G-BA committee
# language. The grounding gate emits a predicted category ONLY if a retrieved chunk
# contains one of its cues. 'other' has no cues (it is the residual).
CATEGORY_CUES: dict[VulnCategory, tuple[str, ...]] = {
    VulnCategory.comparator: (
        "comparator", "did not reflect", "not reflect nhs", "not reflect uk",
        "standard of care", "relevant comparison", "appropriate comparison",
    ),
    VulnCategory.surrogate_endpoint_immaturity: (
        "immature", "not yet mature", "surrogate", "progression-free survival",
        "overall survival data", "os data", "interim analysis", "follow-up was short",
    ),
    VulnCategory.missing_pro: (
        "patient-reported", "quality of life", "health-related quality of life",
        "hrqol", "eq-5d", "utility values",
    ),
    VulnCategory.icer_uncertainty: (
        "icer", "cost-effectiveness estimate", "incremental cost-effectiveness",
        "highly uncertain", "cost effectiveness was uncertain", "above the range",
    ),
    VulnCategory.generalizability: (
        "generalis", "generaliz", "uk clinical practice", "nhs clinical practice",
        "applicability", "trial population", "external validity",
    ),
    VulnCategory.trial_design_bias: (
        "single-arm", "single arm", "open-label", "crossover", "cross-over",
        "risk of bias", "post-hoc", "non-randomised", "naive comparison",
        "indirect comparison", "matching-adjusted",
    ),
    VulnCategory.budget_impact: (
        "budget impact", "affordab", "commercial arrangement", "patient access scheme",
    ),
    VulnCategory.other: (),
}

_LOCK_PATH = Path(__file__).with_name("rubric.lock")


def _canonical() -> str:
    payload = {
        "rubric_version": RUBRIC_VERSION,
        "match_rule": MATCH_RULE,
        "categories": sorted(c.value for c in VulnCategory),
        "cues": {cat.value: sorted(cues) for cat, cues in CATEGORY_CUES.items()},
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=True)


def rubric_hash() -> str:
    """sha256 of the canonical rubric. Changes iff the spec changes."""
    return hashlib.sha256(_canonical().encode("utf-8")).hexdigest()


def commit_rubric() -> str:
    """Write the current rubric hash to rubric.lock (the pre-registration act)."""
    h = rubric_hash()
    _LOCK_PATH.write_text(f"{RUBRIC_VERSION}\n{h}\n")
    return h


def committed_hash() -> str | None:
    if not _LOCK_PATH.exists():
        return None
    lines = _LOCK_PATH.read_text().splitlines()
    return lines[1].strip() if len(lines) >= 2 else None


def assert_rubric_committed() -> None:
    """Invariant #6 gate. Raise unless the live rubric matches the committed hash."""
    recorded = committed_hash()
    if recorded is None:
        raise RuntimeError(
            "rubric not pre-registered: eval/rubric.lock is missing. Run "
            "`python -m strata_platform.eval.rubric --commit` before any scored run."
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
