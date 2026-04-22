"""Stable error-code registry for HTTPException details.

Every user-visible error the API raises ships a detail payload of the shape:

    {"code": "<stable_snake_case>", "message": "<English fallback>"}

plus any call-site-specific extras merged in (e.g. {"until": iso8601} for
lockout responses). The JS client keys its localized toast text off
`code`; curl/API clients see the English `message`. That split keeps
error identity (`code`) stable forever while the human-readable strings
stay editable without breaking integrations.

Localization is intentionally client-side only. Swapping `message` per
Accept-Language would return Japanese JSON to a Japanese user's curl
session, which silently breaks anyone who string-matches on the English
text. Routing translation through the JS layer keeps the server's JSON
shape locale-independent.
"""
from typing import Optional

from fastapi import HTTPException


ERROR_MESSAGES: dict[str, str] = {
    # Auth / session / CSRF
    "invalid_api_token": "Invalid API token.",
    "not_authenticated": "Not authenticated.",
    "missing_origin": "Missing Origin on state-changing request.",
    "cross_origin_blocked": "Cross-origin request blocked.",
    "invalid_credentials": "Invalid credentials.",
    "locked": "Account locked. Try again later.",
    # Sender: request validation
    "field_too_long": "Field too long.",
    "invalid_json_body": "Invalid JSON body.",
    "missing_file": "Missing file.",
    "invalid_expires_in": "Invalid expires_in.",
    "expires_in_not_preset": "expires_in must be one of the allowed presets.",
    "passphrase_too_long": "Passphrase too long.",
    "label_too_long": "Label too long.",
    "file_too_large": "File too large.",
    "unsupported_content_type": "Unsupported content type.",
    "validation_error": "Validation error.",
    # Sender: secret lifecycle
    "not_found": "Not found.",
    "not_found_or_gone": "Not found or already gone.",
    # Receiver: landing / reveal
    "gone": "Secret is no longer available.",
    "passphrase_required": "Passphrase required.",
    "wrong_passphrase": "Wrong passphrase.",
    "too_many_attempts_burned": "Too many attempts. Secret burned.",
    "malformed_key": "Malformed key.",
    "invalid_key_length": "Invalid key length.",
    "decryption_failed": "Decryption failed.",
    # Preferences
    "unsupported_language": "Unsupported language.",
}


def http_error(
    status_code: int,
    code: str,
    *,
    message: Optional[str] = None,
    **extra,
) -> HTTPException:
    """Build an HTTPException with the standard {code, message, **extra}
    detail shape.

    message= overrides the default English when the caller has a more
    specific phrasing (validation errors that embed the bad value, JSON
    parse errors, etc.). **extra merges structural fields -- use for
    things the client needs as separate values, not prose (e.g.
    http_error(423, "locked", until=iso_str) produces
    {"code": "locked", "message": "...", "until": "2026-04-23T..."}).
    """
    body = {"code": code, "message": message or ERROR_MESSAGES[code]}
    if extra:
        body.update(extra)
    return HTTPException(status_code=status_code, detail=body)
