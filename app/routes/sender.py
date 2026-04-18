"""Sender routes: login, logout, secret creation, status lookup."""
from pathlib import Path
from typing import Optional

import bcrypt
from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
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
from ..limiter import create_rate_limit, login_rate_limit
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
    try:
        user = auth_mod.authenticate(
            username, password, code,
            client_ip=security_log.client_ip(request),
        )
    except auth_mod.LockoutError as e:
        raise HTTPException(
            status_code=423,
            detail={"error": "locked", "until": e.until_iso},
        )
    except auth_mod.AuthError:
        raise HTTPException(status_code=401, detail="invalid credentials")

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
            raise HTTPException(status_code=422, detail=f"invalid json body: {e}")
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
            raise HTTPException(status_code=422, detail="missing file")
        try:
            expires_in = int(form.get("expires_in", ""))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="invalid expires_in")
        if expires_in not in EXPIRY_PRESETS:
            raise HTTPException(status_code=422, detail="expires_in must be a preset")
        passphrase = form.get("passphrase") or None
        track = str(form.get("track", "")).lower() in ("1", "true", "on", "yes")
        label = _clean_label(form.get("label"))
        data = await file.read()
        if len(data) > settings.max_image_bytes:
            raise HTTPException(status_code=413, detail="file too large")
        declared = (file.content_type or "").split(";")[0].strip().lower()
        try:
            mime = validation.validate_image(data, declared, settings.max_image_bytes)
        except validation.ValidationError as e:
            raise HTTPException(status_code=400, detail=str(e))
        content_type = "image"
        plaintext = data
    else:
        raise HTTPException(status_code=415, detail="unsupported content type")

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


@router.get("/api/me", response_model=ApiMeResponse)
def api_me(user: dict = Depends(verify_api_token_or_session)):
    """Return a minimal view of the authenticated user (for header UI etc.)."""
    return ApiMeResponse(
        id=user["id"],
        username=user["username"],
        email=user.get("email"),
    )


@router.get("/api/secrets/{sid}/status", response_model=SecretStatusResponse)
def secret_status(sid: str, user: dict = Depends(verify_api_token_or_session)):
    status_row = models.get_status(sid, user["id"])
    if status_row is None:
        raise HTTPException(status_code=404, detail="not found")
    return status_row


@router.get("/api/secrets/tracked", response_model=TrackedListResponse)
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
        raise HTTPException(status_code=404, detail="not found or already gone")
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
