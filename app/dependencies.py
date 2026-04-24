"""FastAPI dependencies: session cookie, bearer auth, origin check.

Session cookies carry the user's id. On login we re-sign with a fresh timestamp
so the cookie value rotates (session-fixation defense). API tokens are keyed to
a user as well. A request is "authenticated as user X" if either credential
resolves to a user row; dependencies below return that row (or raise 401).
"""

from fastapi import Header, Request
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from . import auth as auth_mod
from . import models
from .config import get_settings
from .errors import http_error

# ---------------------------------------------------------------------------
# Session cookie helpers
# ---------------------------------------------------------------------------


def _signer() -> TimestampSigner:
    return TimestampSigner(get_settings().secret_key, salt="ephemera-session")


def make_session_cookie(user_id: int, session_generation: int) -> str:
    """Issue a signed+timestamped cookie binding the cookie to the user's
    current session generation. Re-signing with a fresh timestamp produces a
    new cookie value on every login -> rotation. Bumping the user's
    session_generation invalidates every outstanding cookie."""
    payload = f"{user_id}:{session_generation}".encode()
    return _signer().sign(payload).decode("ascii")


def read_session_cookie(raw: str) -> tuple[int, int] | None:
    """Parse a session cookie to (user_id, generation). Returns None on any
    failure (bad signature, expired, malformed payload)."""
    try:
        max_age = get_settings().session_max_age
        val = _signer().unsign(raw, max_age=max_age).decode("ascii")
        uid_str, gen_str = val.split(":", 1)
        return int(uid_str), int(gen_str)
    except (BadSignature, SignatureExpired, ValueError):
        return None


def current_user_id(request: Request) -> int | None:
    """Return the user id associated with the session cookie, if any. The
    cookie's generation must match the stored `users.session_generation`;
    a mismatch means the session was revoked and the cookie is treated as
    invalid."""
    raw = request.cookies.get(get_settings().session_cookie_name)
    if not raw:
        return None
    parsed = read_session_cookie(raw)
    if parsed is None:
        return None
    uid, gen = parsed
    user = models.get_user_by_id(uid)
    if user is None:
        return None
    if int(user["session_generation"]) != gen:
        return None
    return uid


def is_logged_in(request: Request) -> bool:
    """True if the session cookie identifies a real user and the session has
    not been revoked."""
    return current_user_id(request) is not None


# ---------------------------------------------------------------------------
# API token auth (DB-backed; replaces the old static env API key)
# ---------------------------------------------------------------------------


def verify_api_token_or_session(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    """Accept either a valid DB-issued API token OR a valid session cookie,
    and return the authenticated user row."""
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization.split(" ", 1)[1].strip()
        token_row = auth_mod.lookup_api_token(provided)
        if token_row is not None:
            user = models.get_user_by_id(token_row["user_id"])
            if user is not None:
                return user
        raise http_error(401, "invalid_api_token")

    uid = current_user_id(request)
    if uid is not None:
        user = models.get_user_by_id(uid)
        if user is not None:
            return user
    raise http_error(401, "not_authenticated")


# ---------------------------------------------------------------------------
# Origin check (CSRF defense on state-changing endpoints)
# ---------------------------------------------------------------------------


def verify_same_origin(request: Request) -> None:
    """CSRF defense: Origin must match, or the caller must present a
    *valid* bearer token (CLI/curl flow — no ambient credentials, so no
    CSRF risk).

    Layering: this gate is the CSRF defense for cookie-authenticated
    requests; bearer-auth requests are CSRF-safe regardless of Origin
    because they require a secret the browser can't attach by ambient
    magic. A previous version of this function accepted any string
    prefixed with `Bearer ` and let the downstream auth dependency do
    the rejection at 401 -- net-safe but subtly asymmetric: missing-
    Origin with `Bearer anything` reached the auth layer, whereas
    missing-Origin with a cookie was refused here at 403. Now both
    shapes hit the same gate: if the Authorization header doesn't
    resolve to a real active token, treat it as a browser-case and
    refuse at 403.

    Cost: one extra DB lookup per missing-Origin bearer request. Tokens
    are SHA-256 indexed; the lookup is sub-millisecond. Requests that
    DO carry an Origin header skip this branch entirely.
    """
    origin = request.headers.get("origin")
    if origin is None:
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            provided = auth_header.split(" ", 1)[1].strip()
            if provided and auth_mod.lookup_api_token(provided) is not None:
                return
        raise http_error(403, "missing_origin")
    allowed = get_settings().origins
    if origin not in allowed:
        raise http_error(403, "cross_origin_blocked")
