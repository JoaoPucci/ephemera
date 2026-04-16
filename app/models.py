"""SQLite data layer for ephemera. Plain `def` functions — FastAPI runs them in a threadpool."""
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import get_settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS secrets (
    id            TEXT PRIMARY KEY,
    token         TEXT UNIQUE NOT NULL,
    server_key    BLOB,
    ciphertext    BLOB,
    content_type  TEXT NOT NULL,
    mime_type     TEXT,
    passphrase    TEXT,
    track         INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'pending',
    attempts      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    viewed_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_secrets_token ON secrets(token);
CREATE INDEX IF NOT EXISTS idx_secrets_expires_at ON secrets(expires_at);
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


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(SCHEMA)


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


def create_secret(
    *,
    content_type: str,
    mime_type: Optional[str],
    ciphertext: bytes,
    server_key: bytes,
    passphrase_hash: Optional[str],
    track: bool,
    expires_in: int,
) -> dict:
    now = _utcnow()
    sid = str(uuid.uuid4())
    token = secrets.token_urlsafe(16)
    created_at = _iso(now)
    expires_at = _iso(now + timedelta(seconds=expires_in))

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO secrets (id, token, server_key, ciphertext, content_type,
                                 mime_type, passphrase, track, status, attempts,
                                 created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
            """,
            (sid, token, server_key, ciphertext, content_type, mime_type,
             passphrase_hash, int(bool(track)), created_at, expires_at),
        )
    return {"id": sid, "token": token, "created_at": created_at, "expires_at": expires_at}


def get_by_token(token: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM secrets WHERE token = ?", (token,)).fetchone()
    return _row_to_dict(row) if row else None


def get_by_id(sid: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM secrets WHERE id = ?", (sid,)).fetchone()
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
    """Destroy the payload after too many failed passphrase attempts.

    Untracked secrets are deleted; tracked secrets keep metadata with status='burned'.
    """
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


def get_status(sid: str) -> Optional[dict]:
    """Return status metadata only for tracked secrets; None otherwise."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, status, created_at, expires_at, viewed_at, track FROM secrets WHERE id = ?",
            (sid,),
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
