"""FastAPI dependencies: bearer auth, session cookies, origin checks."""
import hmac
import time
from typing import Optional

from fastapi import Cookie, Header, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from .config import Settings, get_settings


def verify_api_key(
    authorization: Optional[str] = Header(default=None),
) -> None:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    provided = authorization.split(" ", 1)[1].strip()
    expected = get_settings().api_key
    if not hmac.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="invalid api key")


def verify_api_key_or_session(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> None:
    """Accept either an Authorization: Bearer header or a signed session cookie.

    The web form at /send only has a session; external API callers send Bearer.
    """
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization.split(" ", 1)[1].strip()
        expected = get_settings().api_key
        if hmac.compare_digest(provided.encode(), expected.encode()):
            return
        raise HTTPException(status_code=401, detail="invalid api key")
    if is_logged_in(request):
        return
    raise HTTPException(status_code=401, detail="not authenticated")


def _signer() -> TimestampSigner:
    return TimestampSigner(get_settings().secret_key, salt="ephemera-session")


def make_session_cookie(value: str = "ok") -> str:
    return _signer().sign(value).decode("ascii")


def read_session_cookie(raw: str) -> Optional[str]:
    try:
        max_age = get_settings().session_max_age
        value = _signer().unsign(raw, max_age=max_age)
        return value.decode("ascii")
    except (BadSignature, SignatureExpired):
        return None


def require_session(request: Request) -> None:
    settings = get_settings()
    raw = request.cookies.get(settings.session_cookie_name)
    if not raw or read_session_cookie(raw) is None:
        raise HTTPException(status_code=401, detail="no valid session")


def is_logged_in(request: Request) -> bool:
    settings = get_settings()
    raw = request.cookies.get(settings.session_cookie_name)
    return bool(raw and read_session_cookie(raw))


def verify_same_origin(request: Request) -> None:
    """Block reveal POSTs with a foreign Origin header. Missing Origin is allowed
    (older tools, curl in tests) — the client-half requirement acts as a second
    layer of CSRF protection.
    """
    origin = request.headers.get("origin")
    if origin is None:
        return
    allowed = get_settings().origins
    if origin not in allowed:
        raise HTTPException(status_code=403, detail="cross-origin request blocked")
