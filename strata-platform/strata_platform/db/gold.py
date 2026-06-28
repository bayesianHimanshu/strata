"""Load the SME-validated gold set into the ``gold`` table and read it back for scoring.

Invariant #6: gold is expert-validated; eval scores against ``annotator='sme'`` (never a
lexicon-only gold). The bundled ``human_gold.json`` is the human-adjudicated answer key
from the STRATA study.
"""
from __future__ import annotations

from strata_platform.data import load_sme_gold


def load_gold(session, gold: dict[str, list[str]] | None = None,
              annotator: str = "sme") -> int:
    """Idempotently upsert gold rows. Returns the number of decisions written."""
    from strata_platform.db.models import GoldRow

    gold = gold if gold is not None else load_sme_gold()
    for decision_id, categories in gold.items():
        row = session.get(GoldRow, (decision_id, annotator))
        if row is None:
            session.add(GoldRow(decision_id=decision_id, annotator=annotator,
                                categories=list(categories)))
        else:
            row.categories = list(categories)
    session.commit()
    return len(gold)


def read_gold(session, annotator: str = "sme") -> dict[str, set[str]]:
    """Read the gold set as decision_id -> set of category values."""
    from sqlalchemy import select

    from strata_platform.db.models import GoldRow

    stmt = select(GoldRow).where(GoldRow.annotator == annotator)
    return {r.decision_id: set(r.categories) for r in session.execute(stmt).scalars()}
