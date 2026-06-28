"""SME gold loads into the gold table and reads back for scoring (offline, SQLite)."""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from strata_platform.data import load_sme_gold
from strata_platform.db.gold import load_gold, read_gold
from strata_platform.db.models import GoldRow


def _sm():
    engine = create_engine("sqlite://")
    GoldRow.__table__.create(engine)
    return sessionmaker(engine, expire_on_commit=False)


def test_load_and_read_sme_gold_roundtrip() -> None:
    sm = _sm()
    with sm() as s:
        n = load_gold(s)  # bundled human_gold.json
        assert n == 18
    with sm() as s:
        gold = read_gold(s, annotator="sme")
    assert gold["TA1133"] == {"comparator", "icer_uncertainty",
                              "surrogate_endpoint_immaturity", "trial_design_bias"}
    assert gold["TA1130"] == set()  # empty gold is allowed


def test_load_gold_is_idempotent() -> None:
    sm = _sm()
    custom = {"TA1": ["icer_uncertainty"]}
    with sm() as s:
        load_gold(s, custom)
        load_gold(s, {"TA1": ["comparator"]})  # upsert, not duplicate
    with sm() as s:
        gold = read_gold(s)
    assert gold == {"TA1": {"comparator"}}


def test_bundled_gold_matches_data_module() -> None:
    assert set(load_sme_gold()) >= {"TA1133", "TA1156", "TA1146"}
