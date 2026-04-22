"""Operations on the `users` table.

`totp_secret` is encrypted at rest. Writes take plaintext and encrypt
transparently before the INSERT/UPDATE. Raw-SQL callers (tests that read
via sqlite3 directly, or an operator doing forensic triage) see ciphertext
prefixed with `v1:` -- the at-rest invariant this package maintains.

Reads are split in two:

* `get_user_by_id` / `get_user_by_username` are the default accessors.
  They SELECT an explicit column list that OMITS `totp_secret` entirely,
  so the returned dict has no `totp_secret` key. Use these everywhere
  except the paths that genuinely have to verify a TOTP code.
* `get_user_with_totp_by_id` / `get_user_with_totp_by_username` SELECT *
  and return the decrypted base32 plaintext in `row["totp_secret"]`.
  Only three call sites need this: `app.auth.login.authenticate`,
  `app.admin.cmd_diagnose`, and `app.admin.cmd_verify`.

The split keeps the plaintext seed off every session/dependency/admin
read path, so a future log line or error handler that dumps a user dict
can't leak it.
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


# Columns returned by the default (no-TOTP) getters. Enumerated explicitly
# rather than SELECT * so that adding a new column to the users table is a
# deliberate choice about whether it widens the default surface.
_USER_COLUMNS_NO_TOTP = (
    "id, username, email, password_hash, "
    "totp_last_step, recovery_code_hashes, "
    "failed_attempts, lockout_until, session_generation, "
    "preferred_language, "
    "created_at, updated_at"
)


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
    """Fetch a user row WITHOUT `totp_secret`. See module docstring."""
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_USER_COLUMNS_NO_TOTP} FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_user_by_username(username: str) -> Optional[dict]:
    """Fetch a user row WITHOUT `totp_secret`. See module docstring."""
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_USER_COLUMNS_NO_TOTP} FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_user_with_totp_by_id(user_id: int) -> Optional[dict]:
    """Fetch a user row INCLUDING the decrypted TOTP plaintext. Use only
    from code that actually has to verify a TOTP code."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _decrypt_totp(_row_to_dict(row)) if row else None


def get_user_with_totp_by_username(username: str) -> Optional[dict]:
    """Fetch a user row INCLUDING the decrypted TOTP plaintext. Use only
    from code that actually has to verify a TOTP code."""
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


# Whitelist for update_user kwargs. Only columns named here can be set via
# `update_user(**fields)`. Every existing caller passes hardcoded kwargs
# that are on this list; an unknown key indicates either a typo or a
# future caller that tried to pass user-influenced data through. The
# whitelist also means `update_user` never builds a SET clause from a
# name the caller supplied at runtime -- the f-string interpolation over
# `cols` is only reached after the key passes this gate, so a future
# endpoint that did `update_user(uid, **request_body)` couldn't become a
# SQL-injection sink.
_ALLOWED_UPDATE_COLUMNS = frozenset({
    "username",
    "email",
    "password_hash",
    "totp_secret",
    "totp_last_step",
    "recovery_code_hashes",
    "failed_attempts",
    "lockout_until",
    "session_generation",
    "preferred_language",
    # `updated_at` is set by update_user itself, not by callers, but
    # naming it here makes the set the authoritative list of writable
    # columns rather than "everything writable except the one the
    # function itself sets."
    "updated_at",
})


def update_user(user_id: int, **fields) -> None:
    if not fields:
        return
    unknown = set(fields) - _ALLOWED_UPDATE_COLUMNS
    if unknown:
        raise ValueError(
            f"update_user: not a writable column of users: {sorted(unknown)}"
        )
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


def set_preferred_language(user_id: int, language: Optional[str]) -> None:
    """Store the user's preferred UI language (BCP-47 tag like 'ja' or 'pt-BR').
    Passing None clears the preference so locale resolution falls back to the
    request-scoped signals (cookie, Accept-Language, default)."""
    update_user(user_id, preferred_language=language)


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
