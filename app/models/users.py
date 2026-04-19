"""Operations on the `users` table.

`totp_secret` is encrypted at rest. Every read through this module returns
the plaintext base32 string; every write takes plaintext and encrypts
transparently before the INSERT/UPDATE. Raw-SQL callers (tests that read
via sqlite3 directly, or an operator doing forensic triage) see ciphertext
prefixed with `v1:` -- the at-rest invariant this package maintains.
"""
from typing import Optional

from ..crypto import (
    AtRestDecryptionError,
    decrypt_at_rest,
    encrypt_at_rest,
    is_at_rest_ciphertext,
)
from ..security_log import emit as audit
from ._core import _connect, _iso, _row_to_dict, _utcnow


def _decrypt_totp(row_dict: dict) -> dict:
    """Decrypt totp_secret in a row dict. Legacy rows that haven't migrated
    yet (is_at_rest_ciphertext == False) pass through unchanged, which keeps
    the service up through the first boot on a legacy DB -- the migration in
    init_db() handles the rewrite.

    If the ciphertext fails to decrypt (SECRET_KEY rotated out from under us),
    we blank the field rather than 500'ing the caller: TOTP verification will
    then fail cleanly, but the recovery-code rescue path still works, which
    is the documented recovery for exactly this situation.
    """
    sec = row_dict.get("totp_secret")
    if not sec or not is_at_rest_ciphertext(sec):
        return row_dict
    try:
        row_dict["totp_secret"] = decrypt_at_rest(sec)
    except AtRestDecryptionError:
        audit(
            "totp.decrypt_failed",
            user_id=row_dict.get("id"), username=row_dict.get("username"),
        )
        row_dict["totp_secret"] = ""
    return row_dict


def user_count() -> int:
    with _connect() as conn:
        (n,) = conn.execute("SELECT COUNT(*) FROM users").fetchone()
    return int(n)


def get_user_by_id(user_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _decrypt_totp(_row_to_dict(row)) if row else None


def get_user_by_username(username: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return _decrypt_totp(_row_to_dict(row)) if row else None


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
    encrypted_totp = encrypt_at_rest(totp_secret)
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO users (username, email, password_hash, totp_secret,
                                   recovery_code_hashes, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (username, email, password_hash, encrypted_totp, recovery_code_hashes, now, now),
        )
    return int(cur.lastrowid)


def update_user(user_id: int, **fields) -> None:
    if not fields:
        return
    if "totp_secret" in fields and not is_at_rest_ciphertext(fields["totp_secret"]):
        fields["totp_secret"] = encrypt_at_rest(fields["totp_secret"])
    fields["updated_at"] = _iso(_utcnow())
    cols = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [user_id]
    with _connect() as conn:
        conn.execute(f"UPDATE users SET {cols} WHERE id = ?", values)


def delete_user(user_id: int) -> None:
    """Delete a user and (via ON DELETE CASCADE) all their secrets and tokens."""
    with _connect() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


def bump_session_generation(user_id: int) -> int:
    """Invalidate every outstanding session cookie for this user by advancing
    the generation counter the cookie is signed over. Call this after any
    credential rotation (password reset, TOTP rotation, recovery-code regen)
    or when an operator explicitly wants to sign the user out of all devices.

    Returns the new generation value.
    """
    with _connect() as conn:
        row = conn.execute(
            "UPDATE users SET session_generation = session_generation + 1, "
            "updated_at = ? WHERE id = ? RETURNING session_generation",
            (_iso(_utcnow()), user_id),
        ).fetchone()
    return int(row["session_generation"]) if row else 0
