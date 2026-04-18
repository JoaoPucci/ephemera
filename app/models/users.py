"""Operations on the `users` table."""
from typing import Optional

from ._core import _connect, _iso, _row_to_dict, _utcnow


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
