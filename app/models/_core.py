"""Shared data-layer primitives: schema, connection, migrations.

Kept underscore-prefixed so the public `app.models` namespace stays focused
on per-table CRUD. Siblings under this package import `_connect`,
`_utcnow`, `_iso`, `_row_to_dict` from here rather than re-declaring them.
"""
import sqlite3
from datetime import datetime, timezone

from ..config import get_settings


# -----------------------------------------------------------------------------
# Schema
#
# Multi-user-ready: users have real primary keys; secrets and api_tokens are
# scoped to a user via user_id. Fresh installs get this schema directly; older
# single-user DBs are upgraded by the migration in init_db() below.
# -----------------------------------------------------------------------------


TABLES_SCRIPT = """
CREATE TABLE IF NOT EXISTS users (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    username              TEXT NOT NULL,
    email                 TEXT,                           -- nullable until email flows land
    password_hash         TEXT NOT NULL,
    totp_secret           TEXT NOT NULL,
    totp_last_step        INTEGER NOT NULL DEFAULT 0,
    recovery_code_hashes  TEXT NOT NULL DEFAULT '[]',
    failed_attempts       INTEGER NOT NULL DEFAULT 0,
    lockout_until         TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS secrets (
    id            TEXT PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token         TEXT UNIQUE NOT NULL,
    server_key    BLOB,
    ciphertext    BLOB,
    content_type  TEXT NOT NULL,
    mime_type     TEXT,
    passphrase    TEXT,
    track         INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'pending',
    attempts      INTEGER NOT NULL DEFAULT 0,
    label         TEXT,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    viewed_at     TEXT
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    token_hash    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_used_at  TEXT,
    revoked_at    TEXT
);
"""

# Indices are declared separately so migration of legacy DBs can ADD COLUMN first
# and then have the indices built against the new columns.
INDICES_SCRIPT = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email) WHERE email IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_secrets_token ON secrets(token);
CREATE INDEX IF NOT EXISTS idx_secrets_expires_at ON secrets(expires_at);
CREATE INDEX IF NOT EXISTS idx_secrets_user_id ON secrets(user_id);
CREATE INDEX IF NOT EXISTS idx_api_tokens_hash ON api_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_api_tokens_user_id ON api_tokens(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_api_tokens_user_name ON api_tokens(user_id, name);
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(get_settings().db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


def init_db() -> None:
    """Create schema on fresh DBs; migrate legacy single-user DBs in place.

    Order matters: tables first, then ADD COLUMN migrations, then indices --
    otherwise the index creation would race the column additions on a legacy DB.
    """
    with _connect() as conn:
        tables_before = _tables(conn)
        conn.executescript(TABLES_SCRIPT)

        # ---- secrets.label (from an earlier single-user migration) ----
        if "label" not in _cols(conn, "secrets"):
            conn.execute("ALTER TABLE secrets ADD COLUMN label TEXT")

        # ---- Single-user -> multi-user migration ----
        if "users" in tables_before:
            user_cols = _cols(conn, "users")
            if "username" not in user_cols:
                # Default the legacy row to 'admin' so existing CLI workflows keep
                # working. Uniqueness is enforced by the unique index we create
                # below after the column exists.
                conn.execute("ALTER TABLE users ADD COLUMN username TEXT")
                conn.execute("UPDATE users SET username = 'admin' WHERE username IS NULL")
            if "email" not in user_cols:
                conn.execute("ALTER TABLE users ADD COLUMN email TEXT")

        # secrets.user_id: backfill existing rows to user #1 (the legacy owner).
        if "user_id" not in _cols(conn, "secrets"):
            conn.execute("ALTER TABLE secrets ADD COLUMN user_id INTEGER")
            conn.execute("UPDATE secrets SET user_id = 1 WHERE user_id IS NULL")

        # api_tokens.user_id: same.
        if "user_id" not in _cols(conn, "api_tokens"):
            conn.execute("ALTER TABLE api_tokens ADD COLUMN user_id INTEGER")
            conn.execute("UPDATE api_tokens SET user_id = 1 WHERE user_id IS NULL")

        # Indices last, after the columns they reference definitely exist.
        conn.executescript(INDICES_SCRIPT)
