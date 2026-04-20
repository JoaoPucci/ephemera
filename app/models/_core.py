"""Shared data-layer primitives: schema, connection, migrations.

Kept underscore-prefixed so the public `app.models` namespace stays focused
on per-table CRUD. Siblings under this package import `_connect`,
`_utcnow`, `_iso`, `_row_to_dict` from here rather than re-declaring them.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..config import get_settings


class SchemaVersionError(RuntimeError):
    """Raised when the DB schema is at a version the current code doesn't
    know how to run against (typically: operator rolled the code back onto
    a DB that was already upgraded by a newer release). Refusing to boot
    is safer than silently querying with a column layout the code assumes
    is older than it actually is."""


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
    session_generation    INTEGER NOT NULL DEFAULT 0,    -- bumped to invalidate live sessions
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

-- Schema version. Exactly one row (the CHECK pins id=1). Stamped by init_db
-- after all migrations finish. Queried on boot so a downgrade onto a DB at
-- a newer schema version fails loudly instead of quietly running stale code
-- against fresh columns.
CREATE TABLE IF NOT EXISTS schema_version (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    version  INTEGER NOT NULL
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
    db_path = get_settings().db_path
    # Ensure the parent directory exists before sqlite3 tries to create the
    # DB file inside it. Matters on fresh clones where db_path resolves to
    # the XDG default (~/.local/share/ephemera-dev/ephemera.db) and the
    # directory hasn't been created yet. mkdir(exist_ok=True) is a no-op on
    # subsequent calls; the cost is one cheap stat syscall per _connect.
    parent = Path(db_path).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
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


# -----------------------------------------------------------------------------
# Schema versioning + migration registry
#
# Bump CURRENT_SCHEMA_VERSION and register a migration function when adding
# or altering a column. The function takes the open connection and issues
# the SQL that advances the schema from (version-1) to (version). Migrations
# run in order; each applies exactly once per DB.
#
# Keep migrations idempotent where cheap to do so -- it's a safety net if a
# boot is interrupted between the migration and the version-stamp.
#
# v1 is the baseline (everything TABLES_SCRIPT creates + the ad-hoc
# add-column-if-missing blocks in init_db that already existed before the
# registry landed). Legacy DBs that predate this file get stamped to v1 on
# the first boot after upgrade; fresh DBs are stamped to CURRENT on creation.
# -----------------------------------------------------------------------------

CURRENT_SCHEMA_VERSION = 1

_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    # 2: _migrate_to_v2,
    # 3: _migrate_to_v3,
    # ...
}


def _get_schema_version(conn: sqlite3.Connection) -> int:
    """Return the DB's stamped schema version, or 0 if it has never been
    stamped (pre-registry era -- treated as v0 so the version stamp lands
    on the next boot)."""
    row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
    return int(row[0]) if row else 0


def _set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT INTO schema_version (id, version) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET version = excluded.version",
        (version,),
    )


def init_db() -> None:
    """Create schema on fresh DBs; migrate legacy single-user DBs in place.

    Order matters:
      1. Create tables (including schema_version) if they don't exist.
      2. Apply legacy ad-hoc migrations (add-column-if-missing) that brought
         pre-registry DBs up to what is now "v1".
      3. Guard against downgrade: if DB is at a higher version than this code
         knows about, refuse to continue.
      4. Run any registered migrations (_MIGRATIONS) whose target version is
         greater than the current DB version, in ascending order.
      5. Stamp the DB to CURRENT_SCHEMA_VERSION.
      6. Build indices + run in-place data migrations that aren't schema changes.
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
            if "session_generation" not in user_cols:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN session_generation INTEGER NOT NULL DEFAULT 0"
                )

        # secrets.user_id: backfill existing rows to user #1 (the legacy owner).
        if "user_id" not in _cols(conn, "secrets"):
            conn.execute("ALTER TABLE secrets ADD COLUMN user_id INTEGER")
            conn.execute("UPDATE secrets SET user_id = 1 WHERE user_id IS NULL")

        # api_tokens.user_id: same.
        if "user_id" not in _cols(conn, "api_tokens"):
            conn.execute("ALTER TABLE api_tokens ADD COLUMN user_id INTEGER")
            conn.execute("UPDATE api_tokens SET user_id = 1 WHERE user_id IS NULL")

        # ---- Schema-version guard + registered migrations ----
        db_version = _get_schema_version(conn)
        if db_version > CURRENT_SCHEMA_VERSION:
            raise SchemaVersionError(
                f"DB schema is at version {db_version} but this build only "
                f"supports up to {CURRENT_SCHEMA_VERSION}. Upgrade the code "
                "or restore a pre-migration backup -- refusing to run against "
                "a newer schema to avoid silent data corruption."
            )
        for target in sorted(_MIGRATIONS):
            if target > db_version:
                _MIGRATIONS[target](conn)
        _set_schema_version(conn, CURRENT_SCHEMA_VERSION)

        # Indices last, after the columns they reference definitely exist.
        conn.executescript(INDICES_SCRIPT)

        # ---- Encrypt any plaintext totp_secret rows in place ----
        # Detection key: rows prefixed with "v1:" are already encrypted.
        # Anything else is a legacy plaintext base32 TOTP seed and gets
        # rewritten. Idempotent; runs every boot but only touches rows
        # that still need migrating.
        _migrate_plaintext_totp_secrets(conn)


def _migrate_plaintext_totp_secrets(conn: sqlite3.Connection) -> None:
    from ..crypto import encrypt_at_rest, is_at_rest_ciphertext

    rows = conn.execute("SELECT id, totp_secret FROM users").fetchall()
    for r in rows:
        sec = r["totp_secret"] if hasattr(r, "keys") else r[1]
        if not sec or is_at_rest_ciphertext(sec):
            continue
        conn.execute(
            "UPDATE users SET totp_secret = ? WHERE id = ?",
            (encrypt_at_rest(sec), r["id"] if hasattr(r, "keys") else r[0]),
        )
