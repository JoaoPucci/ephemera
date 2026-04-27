"""Shared data-layer primitives: schema, connection, migrations.

Kept underscore-prefixed so the public `app.models` namespace stays focused
on per-table CRUD. Siblings under this package import `_connect`,
`_utcnow`, `_iso`, `_row_to_dict` from here rather than re-declaring them.
"""

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

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
    username              TEXT NOT NULL CHECK (length(username) <= 256),
    email                 TEXT,                           -- nullable until email flows land
    password_hash         TEXT NOT NULL,
    totp_secret           TEXT NOT NULL,
    totp_last_step        INTEGER NOT NULL DEFAULT 0,
    recovery_code_hashes  TEXT NOT NULL DEFAULT '[]',
    failed_attempts       INTEGER NOT NULL DEFAULT 0,
    lockout_until         TEXT,
    session_generation    INTEGER NOT NULL DEFAULT 0,    -- bumped to invalidate live sessions
    preferred_language    TEXT,                           -- BCP-47 tag (e.g. 'ja', 'pt-BR'); NULL = fall back to request signals
    analytics_opt_in      INTEGER NOT NULL DEFAULT 0 CHECK (analytics_opt_in IN (0,1)),  -- 1=user explicitly consented to aggregate-only telemetry; gates emit alongside the operator env. Boolean (not nullable) is deliberate: under opt-in default, "never saw the toggle" and "explicitly declined" are operationally identical -- both mean "do not emit" -- and indistinguishability in the row is the right disclosure posture.
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);

-- CHECK clauses on user-controlled TEXT columns mirror the documented
-- Pydantic ceilings (see app/schemas.py). Defense in depth: if a future
-- write path bypasses the Pydantic boundary, the DB rejects the row
-- rather than storing data that exceeds the application contract.
-- 80 chars on `passphrase` allows headroom over Pydantic's 200-char input
-- limit since the column stores the bcrypt OUTPUT (~60 chars), not the raw
-- passphrase.
CREATE TABLE IF NOT EXISTS secrets (
    id            TEXT PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token         TEXT UNIQUE NOT NULL,
    server_key    BLOB,
    ciphertext    BLOB,
    content_type  TEXT NOT NULL,
    mime_type     TEXT,
    passphrase    TEXT CHECK (passphrase IS NULL OR length(passphrase) <= 80),
    track         INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'pending',
    attempts      INTEGER NOT NULL DEFAULT 0,
    label         TEXT CHECK (label IS NULL OR length(label) <= 60),
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

-- Lightweight event-based analytics. Aggregate-only by design: rows carry
-- no user_id (audit-trail signals belong in security_log.py, not here).
-- Per-event-type payload schema lives in app/analytics.py via EVENT_REGISTRY.
-- See that module's docstring for the privacy invariant ("metadata only,
-- no end-user identity, no payload that could fingerprint an individual").
CREATE TABLE IF NOT EXISTS analytics_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type   TEXT NOT NULL,
    occurred_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    payload      TEXT NOT NULL DEFAULT '{}'
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
CREATE INDEX IF NOT EXISTS idx_analytics_events_type_time
    ON analytics_events(event_type, occurred_at);
"""


def _utcnow() -> datetime:
    return datetime.now(UTC)


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


def ping() -> None:
    """Touch the DB with a no-op query. Raises on open / read failure.

    Surfaces DB-reachability regressions (disk full, perms flipped, WAL
    unwritable, file unlinked) that would otherwise stay invisible until
    the first real request. No rows read, no schema assumed.
    """
    conn = _connect()
    try:
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _row_to_dict(row: sqlite3.Row) -> dict:
    # `row.keys()` is load-bearing here. Iterating a sqlite3.Row yields the
    # *values*, not the column names -- `for k in row` would make `k` a
    # value, and `row[value]` then raises IndexError. Ruff's SIM118 rule
    # flags `in row.keys()` as redundant but is a false positive for
    # sqlite3.Row specifically.
    return {k: row[k] for k in row.keys()}  # noqa: SIM118


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

CURRENT_SCHEMA_VERSION = 6


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    """Add users.preferred_language for localized UI. Idempotent: fresh DBs
    already have the column from TABLES_SCRIPT, so this only fires on legacy
    v1 DBs (or on a boot that was interrupted between this ALTER and the
    version-stamp)."""
    if "preferred_language" not in _cols(conn, "users"):
        conn.execute("ALTER TABLE users ADD COLUMN preferred_language TEXT")


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    """Add CHECK constraints to user-controlled TEXT columns whose Pydantic
    ceiling represents a documented application bound. Affected columns:
    secrets.passphrase (<=80, headroom over the bcrypt output stored here),
    secrets.label (<=60), users.username (<=256).

    SQLite has no `ALTER TABLE ADD CONSTRAINT`, so the affected tables are
    rebuilt via the standard four-step swap (CREATE new -> INSERT SELECT ->
    DROP old -> RENAME new). Indices are recreated by init_db's INDICES_SCRIPT
    after migrations finish, so no per-index work here. Foreign keys are
    re-declared inline; PRAGMA foreign_keys=OFF during the swap so the
    intermediate (table-with-_new-suffix) state doesn't fail FK checks against
    a not-yet-renamed parent.

    Idempotent: fresh DBs already have the CHECK clauses from TABLES_SCRIPT,
    so the introspection guard skips the swap entirely.

    Pre-flight: the current code path that writes user-controlled rows runs
    every value through Pydantic, which already caps to or below the new
    DB-level CHECK limits. So a row that would violate any new CHECK is
    structurally impossible in production. We still count violators and
    abort loudly (rather than failing INSERT mid-migration) -- the failure
    message lists the column and row count so an operator can investigate
    before retrying.
    """
    # Idempotency: detect the new shape via sqlite_master. Fresh DBs land at
    # v3 directly via TABLES_SCRIPT and don't need this rebuild.
    secrets_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='secrets'"
    ).fetchone()
    if secrets_sql and "CHECK" in (secrets_sql[0] or ""):
        return

    # Pre-flight: count rows that would violate any new CHECK. Any nonzero
    # count fails loudly with a remediable message; better that than a
    # half-applied migration when the INSERT below trips a constraint.
    violations = []
    over_passphrase = conn.execute(
        "SELECT COUNT(*) FROM secrets WHERE length(passphrase) > 80"
    ).fetchone()[0]
    if over_passphrase:
        violations.append(
            f"secrets.passphrase: {over_passphrase} row(s) exceed 80 chars"
        )
    over_label = conn.execute(
        "SELECT COUNT(*) FROM secrets WHERE length(label) > 60"
    ).fetchone()[0]
    if over_label:
        violations.append(f"secrets.label: {over_label} row(s) exceed 60 chars")
    over_username = conn.execute(
        "SELECT COUNT(*) FROM users WHERE length(username) > 256"
    ).fetchone()[0]
    if over_username:
        violations.append(f"users.username: {over_username} row(s) exceed 256 chars")
    if violations:
        raise SchemaVersionError(
            "Cannot apply schema v3: existing rows would violate new "
            "CHECK constraints. Investigate manually (or restore a "
            "pre-migration backup) and retry.\n  - " + "\n  - ".join(violations)
        )

    # users.id is AUTOINCREMENT; capture the historical max-issued id from
    # sqlite_sequence BEFORE the destructive swap below. The four-step
    # swap pattern resets the autoincrement counter to MAX(id) of the
    # COPIED rows, which loses the no-reuse guarantee for ids of
    # previously-deleted users. We restore it after RENAME so a future
    # signup never gets recycled into a historical (deleted) user's id --
    # session cookies in this codebase are keyed by user_id+
    # session_generation, so id reuse opens a cookie-replay window.
    #
    # sqlite_sequence is auto-created by SQLite the first time any
    # AUTOINCREMENT column is touched. Pre-multi-user legacy DBs declared
    # `id INTEGER PRIMARY KEY` without AUTOINCREMENT, so the table may
    # not exist on a v0/v1 fixture. Treat its absence as orig_seq=0.
    seq_table_exists = (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
        ).fetchone()
        is not None
    )
    if seq_table_exists:
        row = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='users'"
        ).fetchone()
        orig_users_seq = int(row[0]) if row else 0
    else:
        orig_users_seq = 0

    # Disable FK enforcement for the swap. The intermediate state (where
    # `secrets_new` references `users` and `users_new` exists alongside)
    # would otherwise trip FK checks. _connect re-enables FK on every
    # connection, so the lifetime of this OFF state is bounded to this
    # migration's connection.
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("""
            CREATE TABLE secrets_new (
                id            TEXT PRIMARY KEY,
                user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token         TEXT UNIQUE NOT NULL,
                server_key    BLOB,
                ciphertext    BLOB,
                content_type  TEXT NOT NULL,
                mime_type     TEXT,
                passphrase    TEXT CHECK (passphrase IS NULL OR length(passphrase) <= 80),
                track         INTEGER NOT NULL DEFAULT 0,
                status        TEXT NOT NULL DEFAULT 'pending',
                attempts      INTEGER NOT NULL DEFAULT 0,
                label         TEXT CHECK (label IS NULL OR length(label) <= 60),
                created_at    TEXT NOT NULL,
                expires_at    TEXT NOT NULL,
                viewed_at     TEXT
            )
        """)
        # Named-column INSERT (not SELECT *): legacy DBs that went through the
        # multi-user ALTER ADD COLUMN sequence have columns in a different
        # physical order than TABLES_SCRIPT declares. SELECT * matches by
        # position and would misalign user_id with token, breaking everything.
        conn.execute("""
            INSERT INTO secrets_new (
                id, user_id, token, server_key, ciphertext, content_type,
                mime_type, passphrase, track, status, attempts, label,
                created_at, expires_at, viewed_at
            )
            SELECT
                id, user_id, token, server_key, ciphertext, content_type,
                mime_type, passphrase, track, status, attempts, label,
                created_at, expires_at, viewed_at
            FROM secrets
        """)
        conn.execute("DROP TABLE secrets")
        conn.execute("ALTER TABLE secrets_new RENAME TO secrets")

        conn.execute("""
            CREATE TABLE users_new (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                username              TEXT NOT NULL CHECK (length(username) <= 256),
                email                 TEXT,
                password_hash         TEXT NOT NULL,
                totp_secret           TEXT NOT NULL,
                totp_last_step        INTEGER NOT NULL DEFAULT 0,
                recovery_code_hashes  TEXT NOT NULL DEFAULT '[]',
                failed_attempts       INTEGER NOT NULL DEFAULT 0,
                lockout_until         TEXT,
                session_generation    INTEGER NOT NULL DEFAULT 0,
                preferred_language    TEXT,
                created_at            TEXT NOT NULL,
                updated_at            TEXT NOT NULL
            )
        """)
        # Named columns -- same reason as the secrets INSERT above.
        conn.execute("""
            INSERT INTO users_new (
                id, username, email, password_hash, totp_secret, totp_last_step,
                recovery_code_hashes, failed_attempts, lockout_until,
                session_generation, preferred_language, created_at, updated_at
            )
            SELECT
                id, username, email, password_hash, totp_secret, totp_last_step,
                recovery_code_hashes, failed_attempts, lockout_until,
                session_generation, preferred_language, created_at, updated_at
            FROM users
        """)
        conn.execute("DROP TABLE users")
        conn.execute("ALTER TABLE users_new RENAME TO users")

        # Restore AUTOINCREMENT counter to historical max. ALTER TABLE
        # RENAME updates sqlite_sequence's name field, so the post-RENAME
        # entry for 'users' carries MAX(id) of inserted rows -- which may
        # be lower than orig_users_seq if rows were deleted before the
        # migration. Using UPDATE then INSERT-if-no-row rather than
        # INSERT OR REPLACE because sqlite_sequence has no UNIQUE
        # constraint on `name` (it's a system-managed table); OR REPLACE
        # would silently behave like a plain INSERT and leave duplicate
        # rows behind.
        if orig_users_seq > 0:
            cursor = conn.execute(
                "UPDATE sqlite_sequence SET seq = ? WHERE name = 'users'",
                (orig_users_seq,),
            )
            if cursor.rowcount == 0:
                conn.execute(
                    "INSERT INTO sqlite_sequence (name, seq) VALUES ('users', ?)",
                    (orig_users_seq,),
                )

        # Belt-and-braces: foreign_key_check raises if any FK is dangling
        # after the swap (it shouldn't, since we kept column names + types,
        # but cheap insurance against a typo in the new CREATE TABLE).
        bad_fks = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad_fks:
            raise SchemaVersionError(
                f"v3 migration left dangling FKs: {bad_fks}. Rolling back."
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _migrate_to_v4(conn: sqlite3.Connection) -> None:
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


def _migrate_to_v5(conn: sqlite3.Connection) -> None:
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


def _migrate_to_v6(conn: sqlite3.Connection) -> None:
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


_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    2: _migrate_to_v2,
    3: _migrate_to_v3,
    4: _migrate_to_v4,
    5: _migrate_to_v5,
    6: _migrate_to_v6,
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
                conn.execute(
                    "UPDATE users SET username = 'admin' WHERE username IS NULL"
                )
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
