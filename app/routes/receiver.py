"""Receiver routes: landing, metadata, reveal."""

import base64
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

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


def _load_live_row(token: str) -> dict[str, Any] | None:
    row = models.get_by_token(token)
    if row is None:
        return None
    if row["ciphertext"] is None or row["server_key"] is None:
        return None  # already viewed / burned
    if models.is_expired(row):
        return None
    return row


@router.get("/s/{token}")
def landing_page(token: str, request: Request) -> Response:
    from .. import TEMPLATES

    # chrome_variant="receiver" strips the language picker from the chrome
    # menu (its reload would destroy a revealed one-shot secret) -- see
    # the invariant comment in app/templates/_layout.html near the menu.
    return TEMPLATES.TemplateResponse(
        request,
        "landing.html",
        {**template_context(request), "chrome_variant": "receiver"},
    )


@router.get(
    "/s/{token}/meta",
    response_model=LandingMetaResponse,
    dependencies=[Depends(read_rate_limit)],
)
def landing_meta(token: str) -> LandingMetaResponse:
    row = _load_live_row(token)
    if row is None:
        raise _gone()
    return LandingMetaResponse(passphrase_required=row["passphrase"] is not None)


@router.post(
    "/s/{token}/reveal",
    response_model=RevealResponse,
    dependencies=[Depends(verify_same_origin), Depends(reveal_rate_limit)],
)
def reveal(
    token: str,
    body: RevealBody,
    settings: Settings = Depends(get_settings),
) -> RevealResponse:
    row = _load_live_row(token)
    if row is None:
        raise _gone()

    if row["passphrase"] is not None:
        if not body.passphrase:
            raise http_error(401, "passphrase_required")
        import bcrypt

        # Defense-in-depth length check before bcrypt: Pydantic's
        # RevealBody.passphrase max_length=200 is the primary bound and 422s
        # at the framework boundary, so this guard rarely fires today. It
        # short-circuits the bcrypt cost if a future schema change ever
        # loosens the Pydantic ceiling. Folded into the same failure branch
        # as wrong-passphrase so the wire shape and audit trail are
        # indistinguishable -- per invariant: receiver auth surface MUST NOT
        # differentiate "your input was malformed" from "your input was
        # wrong".
        if len(body.passphrase) > 200 or not bcrypt.checkpw(
            body.passphrase.encode(), row["passphrase"].encode()
        ):
            # Receiver-side audit events: secret_id + the attempts counter
            # is the abuse-detection signal. The receiver's IP is
            # deliberately NOT logged. Receivers are anonymous-by-design
            # in this product (they didn't sign up, they didn't consent
            # to identity capture, they just clicked a link someone
            # sent them); recording their IP next to the secret_id
            # would create a permanent "this IP reached this secret"
            # correlation in journald that doesn't sit anywhere else
            # in the system. The same secret_id repeating across N
            # `reveal.wrong_passphrase` lines tells the operator
            # "secret X is being attacked"; whether the attacker is
            # behind one IP or rotating doesn't change the response
            # (the burn-after-N-fails defense fires on the secret_id,
            # not on the IP).
            attempts = models.increment_attempts(row["id"])
            security_log.emit(
                "reveal.wrong_passphrase",
                secret_id=row["id"],
                attempts=attempts,
            )
            if attempts >= settings.max_passphrase_attempts:
                models.burn(row["id"])
                security_log.emit(
                    "reveal.burned",
                    secret_id=row["id"],
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

    # No `reveal.success` audit event by design. A successful reveal is
    # the product's happy path -- not a security incident -- and emitting
    # one tied to `secret_id` (the only stable identifier we have here,
    # since the receiver is unauthenticated) would create an indefinite
    # "secret X was opened at time T" record in journald with no
    # accountability target to balance it. The DB already records the
    # event in the row's lifecycle (`viewed_at` + status flip on tracked
    # rows; deletion on untracked rows). Symmetric with the deliberate
    # absence of `secret.created`: log destructive / authentication /
    # abuse-shaped events, not happy-path use.

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
