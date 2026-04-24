"""Receiver routes: landing, metadata, reveal."""

import base64

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import crypto, models, security_log
from ..config import Settings, get_settings
from ..dependencies import verify_same_origin
from ..errors import http_error
from ..i18n import template_context
from ..limiter import read_rate_limit, reveal_rate_limit
from ..schemas import (
    LandingMetaResponse,
    RevealBody,
    RevealImageResponse,
    RevealResponse,
    RevealTextResponse,
)

router = APIRouter()


def _gone() -> HTTPException:
    return http_error(404, "gone")


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
def landing_page(token: str, request: Request):
    from .. import TEMPLATES

    return TEMPLATES.TemplateResponse(
        request, "landing.html", template_context(request)
    )


@router.get(
    "/s/{token}/meta",
    response_model=LandingMetaResponse,
    dependencies=[Depends(read_rate_limit)],
)
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
            raise http_error(401, "passphrase_required")
        import bcrypt

        if not bcrypt.checkpw(body.passphrase.encode(), row["passphrase"].encode()):
            attempts = models.increment_attempts(row["id"])
            security_log.emit(
                "reveal.wrong_passphrase",
                secret_id=row["id"],
                client_ip=ip,
                attempts=attempts,
            )
            if attempts >= settings.max_passphrase_attempts:
                models.burn(row["id"])
                security_log.emit(
                    "reveal.burned",
                    secret_id=row["id"],
                    client_ip=ip,
                )
                raise http_error(410, "too_many_attempts_burned")
            raise http_error(401, "wrong_passphrase")

    try:
        client_half = crypto.decode_half(body.key)
    except Exception:
        raise http_error(400, "malformed_key") from None

    if len(client_half) != 16:
        raise http_error(400, "invalid_key_length")

    try:
        full_key = crypto.reconstruct_key(row["server_key"], client_half)
        plaintext = crypto.decrypt(row["ciphertext"], full_key)
    except (crypto.DecryptionError, ValueError):
        raise http_error(400, "decryption_failed") from None

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
