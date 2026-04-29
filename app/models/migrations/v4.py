"""v4: create the analytics_events table for product telemetry."""

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    """Create the analytics_events table for product telemetry. Generic
    enough to absorb future event types without schema changes -- the
    per-event-type schema lives in app/analytics.py via EVENT_REGISTRY.

    Idempotent via CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT
    EXISTS; fresh DBs already have the table from TABLES_SCRIPT (now in
    the v5 shape, no user_id), so this fires for-real only on legacy v3
    DBs upgrading. The user_id column shipped with v4 and is dropped in
    v5; we keep the v4 CREATE TABLE here unchanged so a v3 -> v5 upgrade
    walks through the genuine v4 shape before v5 rewrites it.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS analytics_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type   TEXT NOT NULL,
            occurred_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            user_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
            payload      TEXT NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_analytics_events_type_time "
        "ON analytics_events(event_type, occurred_at)"
    )
