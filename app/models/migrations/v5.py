"""v5: drop analytics_events.user_id (and its FK + index)."""

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    """Drop analytics_events.user_id (and its FK + index). The column was
    added in v4 as a "default shape" for event rows; on reflection, persisting
    user identity past a destroyed secret tensions the product's ephemeral
    pitch and isn't load-bearing for any aggregate metric we'd actually act
    on. Audit-trail signals belong in security_log.py, not analytics.

    SQLite's ALTER TABLE DROP COLUMN can't drop FK-referenced columns cleanly
    on every version we'd want to support, so we use the rename + recreate +
    copy + drop pattern instead.

    Idempotent across an interrupted prior run. If the process was killed
    between the RENAME and the version stamp, the next boot finds:
        * `_analytics_events_v4`: the renamed-but-not-yet-copied real data
        * `analytics_events`: empty, just-recreated by TABLES_SCRIPT (v5 shape)
    Detect that state and skip the rename so we don't either crash on
    "table already exists" or, worse, drop the temp table and lose every
    analytics row. The same recovery branch handles a crash AFTER the
    new-table CREATE but BEFORE the INSERT (v5 table is empty, temp has
    data) and a crash AFTER the INSERT but BEFORE the DROP (v5 table has
    data, temp also has data) -- in both cases the populated v5 table
    gets dropped and re-built from the temp source, idempotent.
    """
    conn.execute("DROP INDEX IF EXISTS idx_analytics_events_user_id")
    has_temp = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_analytics_events_v4'"
    ).fetchone()
    if has_temp:
        # Resume mode: the rename from a prior interrupted run already
        # happened, so _analytics_events_v4 is the authoritative source.
        # Drop whatever sits at analytics_events (empty fresh recreate
        # from TABLES_SCRIPT, or partial-copy from a later crash) and
        # rebuild from the temp.
        conn.execute("DROP TABLE IF EXISTS analytics_events")
    else:
        conn.execute("ALTER TABLE analytics_events RENAME TO _analytics_events_v4")
    conn.execute("""
        CREATE TABLE analytics_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type   TEXT NOT NULL,
            occurred_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            payload      TEXT NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute("""
        INSERT INTO analytics_events (id, event_type, occurred_at, payload)
            SELECT id, event_type, occurred_at, payload FROM _analytics_events_v4
    """)
    conn.execute("DROP TABLE _analytics_events_v4")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_analytics_events_type_time "
        "ON analytics_events(event_type, occurred_at)"
    )
