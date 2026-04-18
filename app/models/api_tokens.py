"""Operations on the `api_tokens` table."""
from typing import Optional

from ._core import _connect, _iso, _row_to_dict, _utcnow


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
