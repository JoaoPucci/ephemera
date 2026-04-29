"""v2: add users.preferred_language for localized UI."""

import sqlite3

from .._core import _cols


def migrate(conn: sqlite3.Connection) -> None:
    """Add users.preferred_language for localized UI. Idempotent: fresh DBs
    already have the column from TABLES_SCRIPT, so this only fires on legacy
    v1 DBs (or on a boot that was interrupted between this ALTER and the
    version-stamp)."""
    if "preferred_language" not in _cols(conn, "users"):
        conn.execute("ALTER TABLE users ADD COLUMN preferred_language TEXT")
