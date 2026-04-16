"""Sender routes: login, logout, secret creation, status lookup."""
from pathlib import Path
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    File,
    status,
)
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from .. import auth as auth_mod
from .. import crypto, models, validation
from ..config import Settings, get_settings
from ..dependencies import (
    is_logged_in,
    make_session_cookie,
    verify_api_token_or_session,
    verify_same_origin,
)
from ..limiter import create_rate_limit, login_rate_limit


router = APIRouter()

EXPIRY_PRESETS = {300, 1800, 3600, 14400, 43200, 86400, 259200, 604800}

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class CreateTextSecret(BaseModel):
    content: str = Field(min_length=1, max_length=1_000_000)
    content_type: str = Field(pattern="^text$")
    expires_in: int
    passphrase: Optional[str] = Field(default=None, max_length=200)
    track: bool = False
    label: Optional[str] = Field(default=None, max_length=60)

    @field_validator("expires_in")
    @classmethod
    def _valid_preset(cls, v: int) -> int:
        if v not in EXPIRY_PRESETS:
            raise ValueError("expires_in must be one of the presets")
        return v


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
    dependencies=[Depends(login_rate_limit), Depends(verify_same_origin)],
)
def send_login(
    request: Request,
    password: str = Form(...),
    code: str = Form(...),
    settings: Settings = Depends(get_settings),
):
    try:
        auth_mod.authenticate(password, code)
    except auth_mod.LockoutError as e:
        # Give the user a hint about how long. Still non-enumerating because
        # lockouts only trigger after many failures.
        raise HTTPException(
            status_code=423,
            detail={"error": "locked", "until": e.until_iso},
        )
    except auth_mod.AuthError:
        raise HTTPException(status_code=401, detail="invalid credentials")

    # Session rotation: new random value on every successful login.
    cookie_value = make_session_cookie()
    response = JSONResponse({"ok": True})
    response.set_cookie(
        key=settings.session_cookie_name,
        value=cookie_value,
        max_age=settings.session_max_age,
        httponly=True,
        samesite="strict",
        secure=False,  # enabled in prod via reverse-proxy-on-HTTPS env
    )
    return response


@router.post("/send/logout", dependencies=[Depends(verify_same_origin)])
def send_logout(settings: Settings = Depends(get_settings)):
    response = JSONResponse({"ok": True})
    response.delete_cookie(settings.session_cookie_name, samesite="strict")
    return response


# ---------------------------------------------------------------------------
# Secret creation + status
# ---------------------------------------------------------------------------


@router.post(
    "/api/secrets",
    status_code=201,
    dependencies=[
        Depends(verify_api_token_or_session),
        Depends(create_rate_limit),
        Depends(verify_same_origin),
    ],
)
async def create_secret(
    request: Request,
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
    import bcrypt as _bcrypt
    passphrase_hash = _bcrypt.hashpw(passphrase.encode(), _bcrypt.gensalt()).decode() if passphrase else None

    row = models.create_secret(
        content_type=content_type,
        mime_type=mime,
        ciphertext=ciphertext,
        server_key=server_half,
        passphrase_hash=passphrase_hash,
        track=bool(track),
        expires_in=int(expires_in),
        label=label if track else None,  # labels are meaningless without tracking
    )

    return {
        "url": _build_url(row["token"], client_half),
        "id": row["id"],
        "expires_at": row["expires_at"],
    }


@router.get(
    "/api/secrets/{sid}/status",
    dependencies=[Depends(verify_api_token_or_session)],
)
def secret_status(sid: str):
    status_row = models.get_status(sid)
    if status_row is None:
        raise HTTPException(status_code=404, detail="not found")
    return status_row


@router.get(
    "/api/secrets/tracked",
    dependencies=[Depends(verify_api_token_or_session)],
)
def list_tracked():
    """List all tracked secrets (server is authoritative; replaces localStorage)."""
    return {"items": models.list_tracked_secrets()}


@router.delete(
    "/api/secrets/{sid}",
    dependencies=[Depends(verify_api_token_or_session), Depends(verify_same_origin)],
)
def untrack_secret(sid: str):
    """Remove a secret from the tracked list.

    If still pending: sets track=0 (URL continues to work).
    If viewed/burned/expired: deletes the row entirely.
    Idempotent: 204 even if the id was already gone.
    """
    models.untrack(sid)
    return Response(status_code=204)
