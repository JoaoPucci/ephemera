"""Operations on the `secrets` table: create, read, reveal, burn, cancel,
expire, purge, and the tracked-list views."""
import secrets as _secrets  # stdlib; aliased to avoid shadowing this module's name
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from ._core import _connect, _iso, _row_to_dict, _utcnow


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
    token = _secrets.token_urlsafe(16)
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


def get_by_id(sid: str, user_id: int) -> Optional[dict]:
    """Lookup by server UUID, scoped to one user.

    user_id is required (was Optional with a None-bypass before; a future
    caller could silently omit it and peek across users). For genuinely
    cross-user admin-only lookups, reach for sqlite3 directly -- the intent
    should be loud at the call site.
    """
    with _connect() as conn:
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
    """UPDATE ... RETURNING in a single statement so the counter we read back
    is the one we just wrote (previously two statements under WAL could let a
    concurrent attempt briefly undercount by one)."""
    with _connect() as conn:
        row = conn.execute(
            "UPDATE secrets SET attempts = attempts + 1 WHERE id = ? RETURNING attempts",
            (sid,),
        ).fetchone()
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
