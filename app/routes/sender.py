"""Sender routes: login, logout, secret creation, status lookup."""
from pathlib import Path
from typing import Optional

import bcrypt
from fastapi import (
    APIRouter,
    Depends,
    Form,
    Request,
    Response,
)
from fastapi.responses import FileResponse

from .. import auth as auth_mod
from .. import crypto, models, security_log, validation
from ..auth import BCRYPT_ROUNDS
from ..config import Settings, get_settings
from ..dependencies import (
    is_logged_in,
    make_session_cookie,
    verify_api_token_or_session,
    verify_same_origin,
)
from ..errors import http_error
from ..limiter import create_rate_limit, login_rate_limit, read_rate_limit
from ..schemas import (
    ApiMeResponse,
    ClearTrackedResponse,
    CreateSecretResponse,
    CreateTextSecret,
    EXPIRY_PRESETS,
    LoginResponse,
    LogoutResponse,
    SecretStatusResponse,
    TrackedListResponse,
)


router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Caps on untyped Form(...) fields. Caddy already limits the body to ~11MB,
# but these save us from spending bcrypt/multipart parsing on obviously
# oversized payloads, and they close off the "FastAPI reachable without
# Caddy" misconfig case.
_MAX_USERNAME_LEN = 256
_MAX_PASSWORD_LEN = 256
_MAX_TOTP_CODE_LEN = 64
_MAX_PASSPHRASE_LEN = 200   # matches CreateTextSecret.passphrase in schemas.py
_MAX_LABEL_LEN = 60         # matches CreateTextSecret.label


def _clean_label(raw) -> Optional[str]:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    return s[:60]


def _build_url(token: str, client_half: bytes) -> str:
    base = get_settings().base_url.rstrip("/")
    encoded = crypto.encode_half(client_half)
    return f"{base}/s/{token}#{encoded}"


# ---------------------------------------------------------------------------
# Web pages (static HTML switched server-side based on session presence)
# ---------------------------------------------------------------------------


@router.get("/send")
def send_page(request: Request):
    page = "sender.html" if is_logged_in(request) else "login.html"
    return FileResponse(STATIC_DIR / page)


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------


@router.post(
    "/send/login",
    response_model=LoginResponse,
    dependencies=[Depends(login_rate_limit), Depends(verify_same_origin)],
)
def send_login(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    code: str = Form(...),
    settings: Settings = Depends(get_settings),
):
    # Reject oversized form fields before spending bcrypt on them. Normal
    # values are well under these caps; anything above is either a typo at
    # the extreme or abuse.
    if (
        len(username) > _MAX_USERNAME_LEN
        or len(password) > _MAX_PASSWORD_LEN
        or len(code) > _MAX_TOTP_CODE_LEN
    ):
        raise http_error(400, "field_too_long")
    try:
        user = auth_mod.authenticate(
            username, password, code,
            client_ip=security_log.client_ip(request),
        )
    except auth_mod.LockoutError as e:
        raise http_error(423, "locked", until=e.until_iso)
    except auth_mod.AuthError:
        raise http_error(401, "invalid_credentials")

    # Session rotation: re-signing with a fresh timestamp gives a new cookie value.
    # The cookie also binds to the user's current session_generation so that
    # rotating credentials (which bumps the counter) invalidates live sessions.
    response.set_cookie(
        key=settings.session_cookie_name,
        value=make_session_cookie(user["id"], int(user["session_generation"])),
        max_age=settings.session_max_age,
        httponly=True,
        samesite="strict",
        secure=settings.session_cookie_secure,
    )
    return LoginResponse(username=user["username"])


@router.post(
    "/send/logout",
    response_model=LogoutResponse,
    dependencies=[Depends(verify_same_origin)],
)
def send_logout(response: Response, settings: Settings = Depends(get_settings)):
    response.delete_cookie(
        settings.session_cookie_name,
        samesite="strict",
        secure=settings.session_cookie_secure,
    )
    return LogoutResponse()


# ---------------------------------------------------------------------------
# Secret creation + status (all scoped to the authenticated user)
# ---------------------------------------------------------------------------


@router.post(
    "/api/secrets",
    status_code=201,
    response_model=CreateSecretResponse,
    dependencies=[
        Depends(create_rate_limit),
        Depends(verify_same_origin),
    ],
)
async def create_secret(
    request: Request,
    user: dict = Depends(verify_api_token_or_session),
    settings: Settings = Depends(get_settings),
):
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()

    label: Optional[str] = None
    if ctype == "application/json":
        try:
            raw = await request.json()
            payload = CreateTextSecret(**raw)
        except Exception as e:
            raise http_error(422, "invalid_json_body", message=f"Invalid JSON body: {e}")
        content_type = "text"
        mime = None
        plaintext = payload.content.encode("utf-8")
        expires_in = payload.expires_in
        passphrase = payload.passphrase
        track = payload.track
        label = _clean_label(payload.label)

    elif ctype == "multipart/form-data":
        form = await request.form()
        file = form.get("file")
        if file is None or not hasattr(file, "read"):
            raise http_error(422, "missing_file")
        try:
            expires_in = int(form.get("expires_in", ""))
        except (TypeError, ValueError):
            raise http_error(422, "invalid_expires_in")
        if expires_in not in EXPIRY_PRESETS:
            raise http_error(422, "expires_in_not_preset")
        passphrase = form.get("passphrase") or None
        if passphrase is not None and len(passphrase) > _MAX_PASSPHRASE_LEN:
            raise http_error(422, "passphrase_too_long")
        track = str(form.get("track", "")).lower() in ("1", "true", "on", "yes")
        raw_label = form.get("label")
        if raw_label is not None and len(str(raw_label)) > _MAX_LABEL_LEN:
            raise http_error(422, "label_too_long")
        label = _clean_label(raw_label)
        data = await file.read()
        if len(data) > settings.max_image_bytes:
            raise http_error(413, "file_too_large")
        declared = (file.content_type or "").split(";")[0].strip().lower()
        try:
            mime = validation.validate_image(data, declared, settings.max_image_bytes)
        except validation.ValidationError as e:
            raise http_error(400, "validation_error", message=str(e))
        content_type = "image"
        plaintext = data
    else:
        raise http_error(415, "unsupported_content_type")

    key = crypto.generate_key()
    server_half, client_half = crypto.split_key(key)
    ciphertext = crypto.encrypt(plaintext, key)
    # Use the project-wide bcrypt cost so a future bump to BCRYPT_ROUNDS
    # applies here too (was silently pinned to the library default before).
    passphrase_hash = (
        bcrypt.hashpw(passphrase.encode(), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()
        if passphrase
        else None
    )

    row = models.create_secret(
        user_id=user["id"],
        content_type=content_type,
        mime_type=mime,
        ciphertext=ciphertext,
        server_key=server_half,
        passphrase_hash=passphrase_hash,
        track=bool(track),
        expires_in=int(expires_in),
        label=label if track else None,  # labels are meaningless without tracking
    )

    return CreateSecretResponse(
        url=_build_url(row["token"], client_half),
        id=row["id"],
        expires_at=row["expires_at"],
    )


@router.get(
    "/api/me",
    response_model=ApiMeResponse,
    dependencies=[Depends(read_rate_limit)],
)
def api_me(user: dict = Depends(verify_api_token_or_session)):
    """Return a minimal view of the authenticated user (for header UI etc.)."""
    return ApiMeResponse(
        id=user["id"],
        username=user["username"],
        email=user.get("email"),
    )


@router.get(
    "/api/secrets/{sid}/status",
    response_model=SecretStatusResponse,
    dependencies=[Depends(read_rate_limit)],
)
def secret_status(sid: str, user: dict = Depends(verify_api_token_or_session)):
    status_row = models.get_status(sid, user["id"])
    if status_row is None:
        raise http_error(404, "not_found")
    return status_row


@router.get(
    "/api/secrets/tracked",
    response_model=TrackedListResponse,
    dependencies=[Depends(read_rate_limit)],
)
def list_tracked(user: dict = Depends(verify_api_token_or_session)):
    """List all tracked secrets owned by the authenticated user."""
    return TrackedListResponse(items=models.list_tracked_secrets(user["id"]))


@router.post(
    "/api/secrets/tracked/clear",
    response_model=ClearTrackedResponse,
    dependencies=[Depends(verify_same_origin)],
)
def clear_tracked_history(user: dict = Depends(verify_api_token_or_session)):
    """Batch-delete every non-pending tracked row for the caller.

    Scope matches what the UI shows as "not pending": viewed, burned,
    canceled, and still-pending-in-DB-but-past-expiry. Pending live rows
    are kept -- they're the user's active secrets.
    """
    count = models.clear_non_pending_tracked(user["id"])
    security_log.emit(
        "secret.cleared",
        user_id=user["id"], username=user["username"], count=count,
    )
    return ClearTrackedResponse(cleared=count)


@router.post(
    "/api/secrets/{sid}/cancel",
    dependencies=[Depends(verify_same_origin)],
)
def cancel_secret(sid: str, user: dict = Depends(verify_api_token_or_session)):
    """Sender revokes a pending secret. Receiver's URL stops working immediately.

    On a currently-live secret: wipes the ciphertext/keys and flags status as
    'canceled' (kept in the tracked list for audit). On anything else: 404.
    """
    if not models.cancel(sid, user["id"]):
        raise http_error(404, "not_found_or_gone")
    security_log.emit(
        "secret.canceled",
        user_id=user["id"], username=user["username"], secret_id=sid,
    )
    return Response(status_code=204)


@router.delete(
    "/api/secrets/{sid}",
    dependencies=[Depends(verify_same_origin)],
)
def untrack_secret(sid: str, user: dict = Depends(verify_api_token_or_session)):
    """Remove a secret from the authenticated user's tracked list.

    Scoped to user_id so one user cannot untrack another's secrets. If still
    pending: sets track=0 (URL continues to work). If viewed/burned/expired:
    deletes the row. Idempotent: 204 even if the id doesn't belong to this user.
    """
    models.untrack(sid, user["id"])
    return Response(status_code=204)
