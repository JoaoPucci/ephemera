"""FastAPI dependencies: bearer auth, session cookies, origin checks."""
from typing import Optional

from fastapi import Header, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from . import auth as auth_mod
from .config import get_settings


# ---------------------------------------------------------------------------
# Session cookie helpers
# ---------------------------------------------------------------------------


def _signer() -> TimestampSigner:
    return TimestampSigner(get_settings().secret_key, salt="ephemera-session")


def make_session_cookie(value: Optional[str] = None) -> str:
    """Issue a fresh signed session cookie. Value is random by default to defeat fixation."""
    if value is None:
        import secrets as _secrets

        value = _secrets.token_urlsafe(16)
    return _signer().sign(value).decode("ascii")


def read_session_cookie(raw: str) -> Optional[str]:
    try:
        max_age = get_settings().session_max_age
        return _signer().unsign(raw, max_age=max_age).decode("ascii")
    except (BadSignature, SignatureExpired):
        return None


def is_logged_in(request: Request) -> bool:
    raw = request.cookies.get(get_settings().session_cookie_name)
    return bool(raw and read_session_cookie(raw))


def require_session(request: Request) -> None:
    if not is_logged_in(request):
        raise HTTPException(status_code=401, detail="no valid session")


# ---------------------------------------------------------------------------
# API token auth (DB-backed; replaces the old static env API key)
# ---------------------------------------------------------------------------


def verify_api_token(authorization: Optional[str] = Header(default=None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    provided = authorization.split(" ", 1)[1].strip()
    row = auth_mod.lookup_api_token(provided)
    if row is None:
        raise HTTPException(status_code=401, detail="invalid api token")
    return row


def verify_api_token_or_session(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> None:
    """Accept either a valid DB-issued API token OR a valid session cookie."""
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization.split(" ", 1)[1].strip()
        if auth_mod.lookup_api_token(provided) is not None:
            return
        raise HTTPException(status_code=401, detail="invalid api token")
    if is_logged_in(request):
        return
    raise HTTPException(status_code=401, detail="not authenticated")


# ---------------------------------------------------------------------------
# Origin check (CSRF defense on state-changing endpoints)
# ---------------------------------------------------------------------------


def verify_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is None:
        return
    allowed = get_settings().origins
    if origin not in allowed:
        raise HTTPException(status_code=403, detail="cross-origin request blocked")
