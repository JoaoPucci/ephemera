"""v6: add users.analytics_opt_in for per-user analytics opt-in."""

import sqlite3

from .._core import _cols


def migrate(conn: sqlite3.Connection) -> None:
    """Add users.analytics_opt_in for per-user analytics opt-in. Default 0
    (opt-in by default; user must explicitly enable). Gates `record_event*`
    emission alongside the operator-level env (`EPHEMERA_ANALYTICS_ENABLED`)
    -- two-gate model. Idempotent: fresh DBs already have the column from
    TABLES_SCRIPT, so this only fires on legacy DBs that landed at v5 before
    the per-user toggle existed."""
    if "analytics_opt_in" not in _cols(conn, "users"):
        conn.execute(
            "ALTER TABLE users ADD COLUMN analytics_opt_in INTEGER "
            "NOT NULL DEFAULT 0 CHECK (analytics_opt_in IN (0,1))"
        )
