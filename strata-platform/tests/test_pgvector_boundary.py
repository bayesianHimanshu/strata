"""The leakage boundary holds against BOTH backends.

InMemoryStore enforces ``boundary.admits`` directly. PgVectorStore compiles the SAME gate
to a SQL WHERE via ``boundary_sql_filter``. This test runs that compiled WHERE on an
in-memory SQLite table over the four canonical boundary cases and asserts the admitted set
is IDENTICAL to ``boundary.admits`` - so leakage is impossible on the production backend
too, proven with no live Postgres. (pgvector's ``<=>`` ordering is Postgres-only and is
exercised separately against a live DB; the WHERE - where leakage lives - is dialect-
neutral and tested here.)
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import Column, Date, MetaData, String, Table, create_engine, select

from strata_platform.substrate.boundary import RetrievalBoundary
from strata_platform.substrate.contracts import Chunk, DocType
from strata_platform.substrate.store import boundary_sql_filter

BOUNDARY = RetrievalBoundary(
    decision_id="TA1133", decision_date=date(2026, 2, 18),
    molecules=frozenset({"belantamab mafodotin"}),
)

CHUNKS = [
    Chunk(chunk_id="own", text="own dossier", doc_type=DocType.ta_final_guidance,
          appraisal_id="TA1133", drug="belantamab mafodotin",
          doc_date=date(2025, 1, 1), source_id="s"),
    Chunk(chunk_id="late", text="post cutoff", doc_type=DocType.literature,
          drug="belantamab mafodotin", doc_date=date(2026, 2, 1), source_id="s"),
    Chunk(chunk_id="ok", text="immature OS surrogate", doc_type=DocType.literature,
          drug="belantamab mafodotin", doc_date=date(2024, 1, 1), source_id="s"),
    Chunk(chunk_id="wrong", text="wrong drug", doc_type=DocType.label,
          drug="elacestrant", doc_date=date(2024, 1, 1), source_id="s"),
]


def _sqlite_admitted(boundary: RetrievalBoundary) -> set[str]:
    md = MetaData()
    t = Table(
        "chunks", md,
        Column("chunk_id", String, primary_key=True),
        Column("text", String),
        Column("doc_type", String),
        Column("appraisal_id", String, nullable=True),
        Column("drug", String, nullable=True),
        Column("doc_date", Date, nullable=True),
    )
    engine = create_engine("sqlite://")
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(t.insert(), [
            {"chunk_id": c.chunk_id, "text": c.text, "doc_type": c.doc_type.value,
             "appraisal_id": c.appraisal_id, "drug": c.drug, "doc_date": c.doc_date}
            for c in CHUNKS
        ])
        rows = conn.execute(
            select(t.c.chunk_id).where(*boundary_sql_filter(t.c, boundary))
        ).all()
    return {r[0] for r in rows}


def test_sql_filter_matches_inmemory_admits_exactly() -> None:
    expected = {c.chunk_id for c in CHUNKS if BOUNDARY.admits(c)}
    assert expected == {"ok"}  # only the eligible chunk
    assert _sqlite_admitted(BOUNDARY) == expected


def test_sql_filter_sibling_exclusion_matches_admits() -> None:
    b = RetrievalBoundary(
        decision_id="TA1133", decision_date=date(2026, 2, 18),
        molecules=frozenset({"belantamab mafodotin"}),
        exclude_siblings=True, sibling_ids=frozenset({"TA1000"}),
    )
    sib_chunk = Chunk(chunk_id="sib", text="sibling prior appraisal",
                      doc_type=DocType.ta_final_guidance, appraisal_id="TA1000",
                      drug="belantamab mafodotin", doc_date=date(2024, 1, 1),
                      source_id="s")
    chunks = [*CHUNKS, sib_chunk]
    md = MetaData()
    t = Table(
        "chunks2", md,
        Column("chunk_id", String, primary_key=True), Column("text", String),
        Column("doc_type", String), Column("appraisal_id", String, nullable=True),
        Column("drug", String, nullable=True), Column("doc_date", Date, nullable=True),
    )
    engine = create_engine("sqlite://")
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(t.insert(), [
            {"chunk_id": c.chunk_id, "text": c.text, "doc_type": c.doc_type.value,
             "appraisal_id": c.appraisal_id, "drug": c.drug, "doc_date": c.doc_date}
            for c in chunks
        ])
        rows = conn.execute(
            select(t.c.chunk_id).where(*boundary_sql_filter(t.c, b))
        ).all()
    sql_admitted = {r[0] for r in rows}
    expected = {c.chunk_id for c in chunks if b.admits(c)}
    assert "sib" not in sql_admitted  # sibling excluded
    assert sql_admitted == expected
