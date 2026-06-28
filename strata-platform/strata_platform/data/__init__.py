"""Bundled reference data: the expert-validated SME gold (``human_gold.json``) and the
NICE decision set (``decisions.json``) from the STRATA study. Loaded into the DB by
``db.gold`` / read directly by the eval harness.
"""
from __future__ import annotations

import json
from pathlib import Path

_DIR = Path(__file__).parent


def load_decisions() -> list[dict]:
    """The 80 NICE oncology decisions (agency, decision_id, decision_date, drug,
    indication, outcome, rationale_raw)."""
    return json.loads((_DIR / "decisions.json").read_text())


def load_sme_gold() -> dict[str, list[str]]:
    """The 18 SME-validated gold items: decision_id -> [VulnCategory value, ...]."""
    return json.loads((_DIR / "human_gold.json").read_text())
