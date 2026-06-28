"""ORM tables for the Azure deployment. Imported lazily by the DB path; not required for
local boot. The chunk embedding column uses pgvector.
"""
from __future__ import annotations

from datetime import date, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, Date, DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SourceRecordRow(Base):
    __tablename__ = "source_records"
    content_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(64))
    source_id: Mapped[str] = mapped_column(String(256))
    url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    doc_type: Mapped[str] = mapped_column(String(64))
    appraisal_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    drug: Mapped[str | None] = mapped_column(String(256), nullable=True)
    doc_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DecisionRow(Base):
    __tablename__ = "decisions"
    decision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="default")
    agency: Mapped[str] = mapped_column(String(32), default="NICE")
    decision_date: Mapped[date] = mapped_column(Date)
    drug: Mapped[str] = mapped_column(String(512))
    indication: Mapped[str] = mapped_column(String(512))
    outcome: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rationale_raw: Mapped[str | None] = mapped_column(Text, nullable=True)


class GoldRow(Base):
    __tablename__ = "gold"
    decision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    annotator: Mapped[str] = mapped_column(String(32), primary_key=True, default="sme")
    categories: Mapped[list] = mapped_column(JSON)


class JobRow(Base):
    __tablename__ = "jobs"
    job_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    capability: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), index=True)
    request: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FaersReportRow(Base):
    """One FAERS spontaneous report (ICSR). Reactions are already MedDRA-coded by openFDA,
    so the signal layer computes disproportionality directly over these."""
    __tablename__ = "faers_report"
    report_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    received_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    serious: Mapped[bool | None] = mapped_column(default=None, nullable=True)


class FaersDrugRow(Base):
    __tablename__ = "faers_drug"
    report_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), primary_key=True)  # suspect|concomitant|interacting


class FaersReactionRow(Base):
    __tablename__ = "faers_reaction"
    report_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    pt: Mapped[str] = mapped_column(String(256), primary_key=True)   # MedDRA preferred term


class ChunkRow(Base):
    __tablename__ = "chunks"
    chunk_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    doc_type: Mapped[str] = mapped_column(String(64), index=True)
    appraisal_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    drug: Mapped[str | None] = mapped_column(String(256), index=True, nullable=True)
    doc_date: Mapped[date | None] = mapped_column(Date, index=True, nullable=True)
    source_id: Mapped[str] = mapped_column(String(256))
    dim: Mapped[int] = mapped_column(Integer, default=1536)
    # pgvector column. The extension + HNSW index are created by Alembic migration #1
    # (CREATE EXTENSION vector; the column type maps to vector(dim)).
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)
