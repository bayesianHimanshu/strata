"""faers tables + vw_signal_metrics disproportionality view (PRR/ROR/signal_flag)

Revision ID: 0002_faers_signal
Revises: 0001_baseline
Create Date: 2026-06-28
"""
from __future__ import annotations

from alembic import op

revision = "0002_faers_signal"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

# Idempotent table creation (the tables may already exist on a fresh DB via the baseline
# create_all; on the already-migrated DB they are created here).
_TABLES = """
CREATE TABLE IF NOT EXISTS faers_report (
  report_id     varchar(64) PRIMARY KEY,
  received_date date,
  serious       boolean
);
CREATE TABLE IF NOT EXISTS faers_drug (
  report_id varchar(64) NOT NULL,
  name      varchar(256) NOT NULL,
  role      varchar(16)  NOT NULL,
  PRIMARY KEY (report_id, name, role)
);
CREATE TABLE IF NOT EXISTS faers_reaction (
  report_id varchar(64)  NOT NULL,
  pt        varchar(256) NOT NULL,
  PRIMARY KEY (report_id, pt)
);
CREATE INDEX IF NOT EXISTS ix_faers_drug_name ON faers_drug (lower(name));
CREATE INDEX IF NOT EXISTS ix_faers_reaction_pt ON faers_reaction (pt);
"""

# Disproportionality view - per (suspect drug, event PT) 2x2 cells, PRR, ROR, signal_flag.
# Ported from VIGIL vw_signal_metrics (BigQuery->Postgres, VAERS->FAERS), generalised to a
# per-drug dimension. signal_flag = PRR>=2 AND a>=3 (conventional screening threshold).
_VIEW = """
CREATE OR REPLACE VIEW vw_signal_metrics AS
WITH case_drug AS (
  SELECT DISTINCT r.report_id, d.name AS scope_drug
  FROM faers_report r JOIN faers_drug d USING (report_id)
  WHERE d.role = 'suspect'
),
case_event AS (
  SELECT DISTINCT report_id, upper(trim(pt)) AS event_pt FROM faers_reaction
  WHERE pt IS NOT NULL AND trim(pt) <> ''
),
drug_totals AS (
  SELECT scope_drug,
         count(DISTINCT report_id) AS n_drug,
         (SELECT count(*) FROM faers_report) - count(DISTINCT report_id) AS n_not_drug
  FROM case_drug GROUP BY scope_drug
),
cells AS (
  SELECT cd.scope_drug, ce.event_pt,
         count(DISTINCT cd.report_id) AS a,
         (SELECT count(DISTINCT ce2.report_id) FROM case_event ce2
            WHERE ce2.event_pt = ce.event_pt
              AND ce2.report_id NOT IN
                  (SELECT report_id FROM case_drug WHERE scope_drug = cd.scope_drug)
         ) AS c
  FROM case_drug cd JOIN case_event ce USING (report_id)
  GROUP BY cd.scope_drug, ce.event_pt
)
SELECT c.scope_drug, c.event_pt, c.a,
       (dt.n_drug - c.a)                                   AS b,
       c.c,
       (dt.n_not_drug - c.c)                               AS d,
       dt.n_drug + dt.n_not_drug                           AS n_total,
       round((c.a::numeric / nullif(dt.n_drug, 0)) /
             nullif(c.c::numeric / nullif(dt.n_not_drug, 0), 0), 3)          AS prr,
       round((c.a::numeric * (dt.n_not_drug - c.c)) /
             nullif((dt.n_drug - c.a) * c.c, 0), 3)                          AS ror,
       ((c.a::numeric / nullif(dt.n_drug, 0)) /
        nullif(c.c::numeric / nullif(dt.n_not_drug, 0), 0) >= 2 AND c.a >= 3) AS signal_flag
FROM cells c JOIN drug_totals dt USING (scope_drug)
ORDER BY prr DESC NULLS LAST;
"""


def upgrade() -> None:
    op.execute(_TABLES)
    op.execute(_VIEW)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS vw_signal_metrics")
    op.execute("DROP TABLE IF EXISTS faers_reaction")
    op.execute("DROP TABLE IF EXISTS faers_drug")
    op.execute("DROP TABLE IF EXISTS faers_report")
