"""FastAPI dependencies: session cookie, bearer auth, origin check.

Session cookies carry the user's id. On login we re-sign with a fresh timestamp
so the cookie value rotates (session-fixation defense). API tokens are keyed to
a user as well. A request is "authenticated as user X" if either credential
resolves to a user row; dependencies below return that row (or raise 401).
"""
from typing import Optional

from fastapi import Header, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from . import auth as auth_mod
from . import models
from .config import get_settings


# ---------------------------------------------------------------------------
# Session cookie helpers
# ---------------------------------------------------------------------------


def _signer() -> TimestampSigner:
    return TimestampSigner(get_settings().secret_key, salt="ephemera-session")


def make_session_cookie(user_id: int) -> str:
    """Issue a signed+timestamped cookie carrying the user id. Re-signing with a
    fresh timestamp produces a new cookie value on every login -> rotation."""
    return _signer().sign(str(user_id).encode()).decode("ascii")


def read_session_cookie(raw: str) -> Optional[int]:
    try:
        max_age = get_settings().session_max_age
        val = _signer().unsign(raw, max_age=max_age).decode("ascii")
        return int(val)
    except (BadSignature, SignatureExpired, ValueError):
        return None


def current_user_id(request: Request) -> Optional[int]:
    """Return the user id associated with the session cookie, if any."""
    raw = request.cookies.get(get_settings().session_cookie_name)
    return read_session_cookie(raw) if raw else None


def is_logged_in(request: Request) -> bool:
    """True if the session cookie identifies a real user.

    Checks that the user row still exists, so a deleted user's stale cookie
    doesn't keep working.
    """
    uid = current_user_id(request)
    if uid is None:
        return False
    return models.get_user_by_id(uid) is not None


# ---------------------------------------------------------------------------
# API token auth (DB-backed; replaces the old static env API key)
# ---------------------------------------------------------------------------


def verify_api_token_or_session(
    request: Request,
    authorization: Optional[str] = Header(default=None),
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
        raise HTTPException(status_code=401, detail="invalid api token")

    uid = current_user_id(request)
    if uid is not None:
        user = models.get_user_by_id(uid)
        if user is not None:
            return user
    raise HTTPException(status_code=401, detail="not authenticated")


# ---------------------------------------------------------------------------
# Origin check (CSRF defense on state-changing endpoints)
# ---------------------------------------------------------------------------


def verify_same_origin(request: Request) -> None:
    """CSRF defense: Origin must match, or the caller must be using a bearer
    token (CLI/curl flow — no ambient credentials, no CSRF risk).

    Missing-Origin requests from browsers are refused here, because missing
    Origin + a session cookie = the exact shape of the CSRF gap in F-03.
    Historically this function returned early on missing Origin to keep
    CLI clients working; CLI clients use bearer auth and still do.
    """
    origin = request.headers.get("origin")
    if origin is None:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return
        raise HTTPException(status_code=403, detail="missing origin on state-changing request")
    allowed = get_settings().origins
    if origin not in allowed:
        raise HTTPException(status_code=403, detail="cross-origin request blocked")
