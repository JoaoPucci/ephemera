"""API tokens: DB-backed, per-user, revocable. Replaces the old static
EPHEMERA_API_KEY env var. Only SHA-256(plaintext) is stored server-side."""

import hashlib
import secrets

from .. import models

TOKEN_PREFIX = "eph_"


def mint_api_token() -> tuple[str, str]:
    """Return (plaintext_token, sha256_hash). Plaintext shown to user once."""
    body = secrets.token_urlsafe(32)
    plaintext = TOKEN_PREFIX + body
    digest = hashlib.sha256(plaintext.encode()).hexdigest()
    return plaintext, digest


def lookup_api_token(plaintext: str) -> dict | None:
    """Return the token row (with user_id) if valid; None otherwise.

    On hit, touches last_used_at.
    """
    if not plaintext or not plaintext.startswith(TOKEN_PREFIX):
        return None
    digest = hashlib.sha256(plaintext.encode()).hexdigest()
    row = models.get_active_token_by_hash(digest)
    if row is None:
        return None
    models.touch_token_last_used(row["id"])
    return row
