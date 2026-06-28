"""Read-only SQL guard for the signal capability (ported from VIGIL, Postgres dialect).

The signal capability generates SQL via the model; this validates it BEFORE execution.
A query is accepted only if it is a single ``SELECT``, contains no write/DDL operations,
references only allow-listed relations (plus its own CTEs), and carries a ``LIMIT`` (one is
appended if missing). Deny-by-default. Pure (sqlglot only) and unit-testable.
"""
from __future__ import annotations

from collections.abc import Iterable

import sqlglot
from sqlglot import exp

_FORBIDDEN = (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create,
              exp.Merge, exp.Alter, exp.Command, exp.Into)


def validate_readonly(sql: str, allowed_tables: Iterable[str],
                      default_limit: int = 100) -> tuple[bool, str, str | None]:
    """Return (ok, reason, safe_sql). ``safe_sql`` is normalised with a LIMIT and only set
    when ok. Deny-by-default: anything not provably a single read-only SELECT against the
    allow-list is rejected."""
    allowed = {t.lower() for t in allowed_tables}
    try:
        stmts = [s for s in sqlglot.parse(sql, dialect="postgres") if s is not None]
    except Exception as e:  # noqa: BLE001
        return False, f"unparseable: {str(e).splitlines()[0][:80]}", None

    if len(stmts) != 1:
        return False, "exactly one statement is allowed", None
    tree = stmts[0]

    if any(tree.find(node) for node in _FORBIDDEN):
        return False, "write/DDL operation not allowed", None
    if not isinstance(tree, exp.Select):
        return False, "only a single SELECT is allowed", None

    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    referenced = {t.name.lower() for t in tree.find_all(exp.Table)}
    disallowed = referenced - allowed - cte_names
    if disallowed:
        return False, f"references disallowed objects: {sorted(disallowed)}", None

    if tree.args.get("limit") is None:
        tree = tree.limit(default_limit)
    return True, "ok", tree.sql(dialect="postgres")
