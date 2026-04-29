"""v3: add CHECK constraints to user-controlled TEXT columns whose Pydantic
ceiling represents a documented application bound."""

import sqlite3

from .._core import SchemaVersionError


def migrate(conn: sqlite3.Connection) -> None:
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
    else:  # pragma: no cover -- defensive fallback; see comment below
        # Pre-multi-user v0/v1 legacy DBs that declared `id INTEGER PRIMARY
        # KEY` without AUTOINCREMENT never created sqlite_sequence, so this
        # branch fires for them. Driving it from a test fixture requires
        # constructing a DB that has no AUTOINCREMENT-using table at all
        # (SQLite refuses to DROP sqlite_sequence once it exists), and the
        # subsequent state shape diverges enough from v2 that the rest of
        # the migration covers no useful behaviour delta. The no-reuse
        # guarantee that this whole block protects is tested by
        # test_v3_migration_preserves_users_autoincrement_no_reuse.
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
            if cursor.rowcount == 0:  # pragma: no cover -- defensive
                # SQLite versions vary on whether DROP TABLE+RENAME
                # preserve the sqlite_sequence row through the swap. The
                # UPDATE above is the common path; this INSERT is the
                # fallback for the rare case where the row was lost.
                # Driving it deterministically from a test would require
                # mid-migration inspection of sqlite_sequence; the
                # no-reuse end-state contract is pinned by
                # test_v3_migration_preserves_users_autoincrement_no_reuse.
                conn.execute(
                    "INSERT INTO sqlite_sequence (name, seq) VALUES ('users', ?)",
                    (orig_users_seq,),
                )

        # Belt-and-braces: foreign_key_check raises if any FK is dangling
        # after the swap (it shouldn't, since we kept column names + types,
        # but cheap insurance against a typo in the new CREATE TABLE). The
        # raise + the broader except/ROLLBACK below are intentionally not
        # exercised by the test suite -- driving them requires injecting a
        # bug into the v3 migration itself, which would never survive code
        # review. They exist as a safety net, not a tested code path.
        bad_fks = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad_fks:  # pragma: no cover -- defensive; see comment above
            raise SchemaVersionError(
                f"v3 migration left dangling FKs: {bad_fks}. Rolling back."
            )
        conn.execute("COMMIT")
    except Exception:  # pragma: no cover -- defensive; see comment above
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
