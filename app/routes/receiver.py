"""Receiver routes: landing, metadata, reveal."""
import base64
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from .. import crypto, models, security_log
from ..config import Settings, get_settings
from ..dependencies import verify_same_origin
from ..limiter import reveal_rate_limit
from ..schemas import (
    LandingMetaResponse,
    RevealBody,
    RevealImageResponse,
    RevealResponse,
    RevealTextResponse,
)


router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _gone() -> HTTPException:
    return HTTPException(status_code=404, detail="gone")


def _load_live_row(token: str):
    row = models.get_by_token(token)
    if row is None:
        return None
    if row["ciphertext"] is None or row["server_key"] is None:
        return None  # already viewed / burned
    if models.is_expired(row):
        return None
    return row


@router.get("/s/{token}")
def landing_page(token: str):
    return FileResponse(STATIC_DIR / "landing.html")


@router.get("/s/{token}/meta", response_model=LandingMetaResponse)
def landing_meta(token: str):
    row = _load_live_row(token)
    if row is None:
        raise _gone()
    return LandingMetaResponse(passphrase_required=row["passphrase"] is not None)


@router.post(
    "/s/{token}/reveal",
    response_model=RevealResponse,
    dependencies=[Depends(reveal_rate_limit), Depends(verify_same_origin)],
)
def reveal(
    request: Request,
    token: str,
    body: RevealBody,
    settings: Settings = Depends(get_settings),
):
    ip = security_log.client_ip(request)
    row = _load_live_row(token)
    if row is None:
        raise _gone()

    if row["passphrase"] is not None:
        if not body.passphrase:
            raise HTTPException(status_code=401, detail="passphrase required")
        import bcrypt

        if not bcrypt.checkpw(body.passphrase.encode(), row["passphrase"].encode()):
            attempts = models.increment_attempts(row["id"])
            security_log.emit(
                "reveal.wrong_passphrase",
                secret_id=row["id"], client_ip=ip, attempts=attempts,
            )
            if attempts >= settings.max_passphrase_attempts:
                models.burn(row["id"])
                security_log.emit(
                    "reveal.burned",
                    secret_id=row["id"], client_ip=ip,
                )
                raise HTTPException(status_code=410, detail="too many attempts, secret burned")
            raise HTTPException(status_code=401, detail="wrong passphrase")

    try:
        client_half = crypto.decode_half(body.key)
    except Exception:
        raise HTTPException(status_code=400, detail="malformed key")

    if len(client_half) != 16:
        raise HTTPException(status_code=400, detail="invalid key length")

    try:
        full_key = crypto.reconstruct_key(row["server_key"], client_half)
        plaintext = crypto.decrypt(row["ciphertext"], full_key)
    except (crypto.DecryptionError, ValueError):
        raise HTTPException(status_code=400, detail="decryption failed")

    # Atomically claim the row; if a concurrent reveal already won, 404 and
    # discard the plaintext we decrypted. See models.consume_for_reveal.
    if not models.consume_for_reveal(row["id"], track=bool(row["track"])):
        raise _gone()

    security_log.emit("reveal.success", secret_id=row["id"], client_ip=ip)

    if row["content_type"] == "image":
        return RevealImageResponse(
            content_type="image",
            mime_type=row["mime_type"],
            content=base64.b64encode(plaintext).decode("ascii"),
        )
    return RevealTextResponse(
        content_type="text",
        content=plaintext.decode("utf-8"),
    )
