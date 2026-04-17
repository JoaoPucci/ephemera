"""SQLite data layer for ephemera. Plain `def` functions — FastAPI runs them in a threadpool."""
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import get_settings


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


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


# -----------------------------------------------------------------------------
# secrets
# -----------------------------------------------------------------------------


def create_secret(
    *,
    user_id: int,
    content_type: str,
    mime_type: Optional[str],
    ciphertext: bytes,
    server_key: bytes,
    passphrase_hash: Optional[str],
    track: bool,
    expires_in: int,
    label: Optional[str] = None,
) -> dict:
    now = _utcnow()
    sid = str(uuid.uuid4())
    token = secrets.token_urlsafe(16)
    created_at = _iso(now)
    expires_at = _iso(now + timedelta(seconds=expires_in))

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO secrets (id, user_id, token, server_key, ciphertext, content_type,
                                 mime_type, passphrase, track, status, attempts,
                                 label, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
            """,
            (sid, user_id, token, server_key, ciphertext, content_type, mime_type,
             passphrase_hash, int(bool(track)), label, created_at, expires_at),
        )
    return {"id": sid, "token": token, "created_at": created_at, "expires_at": expires_at}


def get_by_token(token: str) -> Optional[dict]:
    """Lookup by the URL-facing token. Intentionally not user-scoped: the receiver
    has no user identity and must be able to reach their secret via the link."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM secrets WHERE token = ?", (token,)).fetchone()
    return _row_to_dict(row) if row else None


def get_by_id(sid: str, user_id: Optional[int] = None) -> Optional[dict]:
    """Lookup by server UUID. Pass user_id to prevent cross-user peeking."""
    with _connect() as conn:
        if user_id is None:
            row = conn.execute("SELECT * FROM secrets WHERE id = ?", (sid,)).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM secrets WHERE id = ? AND user_id = ?", (sid, user_id)
            ).fetchone()
    return _row_to_dict(row) if row else None


def delete_secret(sid: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM secrets WHERE id = ?", (sid,))


def mark_viewed(sid: str) -> None:
    """Reveal the secret: delete if untracked, null-out payload if tracked."""
    now = _iso(_utcnow())
    with _connect() as conn:
        row = conn.execute("SELECT track FROM secrets WHERE id = ?", (sid,)).fetchone()
        if row is None:
            return
        if row["track"]:
            conn.execute(
                """
                UPDATE secrets
                   SET ciphertext = NULL,
                       server_key = NULL,
                       passphrase = NULL,
                       status     = 'viewed',
                       viewed_at  = ?
                 WHERE id = ?
                """,
                (now, sid),
            )
        else:
            conn.execute("DELETE FROM secrets WHERE id = ?", (sid,))


def burn(sid: str) -> None:
    """Destroy the payload after too many failed passphrase attempts."""
    with _connect() as conn:
        row = conn.execute("SELECT track FROM secrets WHERE id = ?", (sid,)).fetchone()
        if row is None:
            return
        if row["track"]:
            conn.execute(
                """
                UPDATE secrets
                   SET ciphertext = NULL,
                       server_key = NULL,
                       passphrase = NULL,
                       status     = 'burned',
                       viewed_at  = ?
                 WHERE id = ?
                """,
                (_iso(_utcnow()), sid),
            )
        else:
            conn.execute("DELETE FROM secrets WHERE id = ?", (sid,))


def increment_attempts(sid: str) -> int:
    with _connect() as conn:
        conn.execute("UPDATE secrets SET attempts = attempts + 1 WHERE id = ?", (sid,))
        row = conn.execute("SELECT attempts FROM secrets WHERE id = ?", (sid,)).fetchone()
    return int(row["attempts"]) if row else 0


def get_status(sid: str, user_id: int) -> Optional[dict]:
    """Return status metadata for a tracked secret that belongs to this user."""
    with _connect() as conn:
        row = conn.execute(
            """SELECT status, created_at, expires_at, viewed_at, track
                 FROM secrets WHERE id = ? AND user_id = ?""",
            (sid, user_id),
        ).fetchone()
    if row is None or not row["track"]:
        return None
    return {
        "status": row["status"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "viewed_at": row["viewed_at"],
    }


def is_expired(row: dict) -> bool:
    expires_at = datetime.strptime(row["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return _utcnow() >= expires_at


def purge_expired() -> int:
    now = _iso(_utcnow())
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM secrets WHERE expires_at <= ? AND (status = 'pending' OR track = 0)",
            (now,),
        )
    return cur.rowcount or 0


def purge_tracked_metadata(retention_seconds: int) -> int:
    cutoff = _iso(_utcnow() - timedelta(seconds=retention_seconds))
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM secrets WHERE track = 1 AND viewed_at IS NOT NULL AND viewed_at <= ?",
            (cutoff,),
        )
    return cur.rowcount or 0


def _force_viewed_at(sid: str, viewed_at: str) -> None:
    """Test helper: overwrite viewed_at so retention logic can be exercised."""
    with _connect() as conn:
        conn.execute("UPDATE secrets SET viewed_at = ? WHERE id = ?", (viewed_at, sid))


def list_tracked_secrets(user_id: int) -> list[dict]:
    """Return all tracked secrets owned by user_id, newest first."""
    now_iso = _iso(_utcnow())
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, content_type, mime_type, label, status, created_at, expires_at, viewed_at,
                   CASE
                     WHEN status = 'viewed'   THEN 'viewed'
                     WHEN status = 'burned'   THEN 'burned'
                     WHEN status = 'canceled' THEN 'canceled'
                     WHEN expires_at <= ?     THEN 'expired'
                     ELSE 'pending'
                   END AS effective_status
              FROM secrets
             WHERE track = 1 AND user_id = ?
             ORDER BY created_at DESC
            """,
            (now_iso, user_id),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "content_type": r["content_type"],
            "mime_type": r["mime_type"],
            "label": r["label"],
            "status": r["effective_status"],
            "created_at": r["created_at"],
            "expires_at": r["expires_at"],
            "viewed_at": r["viewed_at"],
        }
        for r in rows
    ]


def cancel(sid: str, user_id: int) -> bool:
    """Sender-initiated revocation of their own still-live secret.

    Wipes payload + key material (same shape as burn()), but tags status as
    'canceled' so the tracked-list UI can distinguish "sender changed their
    mind" from "someone smashed the passphrase". Idempotent-ish: returns False
    if the secret doesn't exist, belongs to another user, or was never live
    (already viewed/burned/expired/canceled).
    """
    with _connect() as conn:
        row = conn.execute(
            """SELECT track, ciphertext FROM secrets
                WHERE id = ? AND user_id = ?""",
            (sid, user_id),
        ).fetchone()
        if row is None or row["ciphertext"] is None:
            return False
        if row["track"]:
            conn.execute(
                """
                UPDATE secrets
                   SET ciphertext = NULL,
                       server_key = NULL,
                       passphrase = NULL,
                       status     = 'canceled',
                       viewed_at  = ?
                 WHERE id = ?
                """,
                (_iso(_utcnow()), sid),
            )
        else:
            conn.execute("DELETE FROM secrets WHERE id = ?", (sid,))
    return True


def clear_non_pending_tracked(user_id: int) -> int:
    """Delete every tracked row owned by user_id that is no longer live:
    viewed, burned, canceled, or still-'pending' but past expiry. Returns
    the number of rows removed. Pending-and-unexpired rows are kept."""
    now = _iso(_utcnow())
    with _connect() as conn:
        cur = conn.execute(
            """
            DELETE FROM secrets
             WHERE user_id = ?
               AND track = 1
               AND (
                 status IN ('viewed', 'burned', 'canceled')
                 OR (status = 'pending' AND expires_at <= ?)
               )
            """,
            (user_id, now),
        )
    return cur.rowcount or 0


def untrack(sid: str, user_id: int) -> bool:
    """Stop showing this secret in the tracked list. Scoped to user_id so one
    user cannot untrack another's secrets."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, ciphertext FROM secrets WHERE id = ? AND user_id = ?",
            (sid, user_id),
        ).fetchone()
        if row is None:
            return False
        if row["ciphertext"] is None:
            conn.execute("DELETE FROM secrets WHERE id = ?", (sid,))
        else:
            conn.execute("UPDATE secrets SET track = 0 WHERE id = ?", (sid,))
    return True


# -----------------------------------------------------------------------------
# users
# -----------------------------------------------------------------------------


def user_count() -> int:
    with _connect() as conn:
        (n,) = conn.execute("SELECT COUNT(*) FROM users").fetchone()
    return int(n)


def get_user_by_id(user_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _row_to_dict(row) if row else None


def get_user_by_username(username: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return _row_to_dict(row) if row else None


def list_users() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, username, email, created_at, updated_at FROM users ORDER BY id"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def create_user(
    *,
    username: str,
    password_hash: str,
    totp_secret: str,
    recovery_code_hashes: str,
    email: Optional[str] = None,
) -> int:
    now = _iso(_utcnow())
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO users (username, email, password_hash, totp_secret,
                                   recovery_code_hashes, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (username, email, password_hash, totp_secret, recovery_code_hashes, now, now),
        )
    return int(cur.lastrowid)


def update_user(user_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = _iso(_utcnow())
    cols = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [user_id]
    with _connect() as conn:
        conn.execute(f"UPDATE users SET {cols} WHERE id = ?", values)


def delete_user(user_id: int) -> None:
    """Delete a user and (via ON DELETE CASCADE) all their secrets and tokens."""
    with _connect() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


# -----------------------------------------------------------------------------
# api_tokens
# -----------------------------------------------------------------------------


def list_tokens(user_id: int) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT id, user_id, name, created_at, last_used_at, revoked_at
                 FROM api_tokens WHERE user_id = ? ORDER BY id""",
            (user_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def create_token(*, user_id: int, name: str, token_hash: str) -> None:
    now = _iso(_utcnow())
    with _connect() as conn:
        conn.execute(
            "INSERT INTO api_tokens (user_id, name, token_hash, created_at) VALUES (?, ?, ?, ?)",
            (user_id, name, token_hash, now),
        )


def revoke_token(user_id: int, name: str) -> bool:
    now = _iso(_utcnow())
    with _connect() as conn:
        cur = conn.execute(
            """UPDATE api_tokens SET revoked_at = ?
                WHERE user_id = ? AND name = ? AND revoked_at IS NULL""",
            (now, user_id, name),
        )
    return (cur.rowcount or 0) > 0


def get_active_token_by_hash(token_hash: str) -> Optional[dict]:
    """Return the token row with its user_id. Not user-scoped because this IS
    how we find the user from the token."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM api_tokens WHERE token_hash = ? AND revoked_at IS NULL",
            (token_hash,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def touch_token_last_used(token_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (_iso(_utcnow()), token_id)
        )
