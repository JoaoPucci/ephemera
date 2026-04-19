"""TOTP per RFC 6238, via pyotp. +/-1 step tolerance with anti-replay."""
import hmac
import time
from typing import Optional

import pyotp

from ._core import TOTP_DIGITS, TOTP_INTERVAL, TOTP_STEP_TOLERANCE


def generate_totp_secret() -> str:
    return pyotp.random_base32(length=32)


def provisioning_uri(secret: str, account_name: str = "admin", issuer: str = "ephemera") -> str:
    return pyotp.TOTP(secret, digits=TOTP_DIGITS, interval=TOTP_INTERVAL).provisioning_uri(
        name=account_name, issuer_name=issuer
    )


def _current_step() -> int:
    return int(time.time()) // TOTP_INTERVAL


def verify_totp(secret: str, code: str, last_step: int) -> Optional[int]:
    """Return the accepted step on success (caller should persist it as the
    new `last_step` to prevent replay), or None on failure / rejected code.

    An empty `secret` is treated as a rejection rather than an error. That's
    the shape models.users._decrypt_totp leaves behind when the stored
    TOTP ciphertext can't be decrypted (SECRET_KEY rotated); keeping this
    branch quiet lets the caller fall through to the recovery-code path."""
    if not secret:
        return None
    if not code.isdigit() or len(code) != TOTP_DIGITS:
        return None
    totp = pyotp.TOTP(secret, digits=TOTP_DIGITS, interval=TOTP_INTERVAL)
    now = _current_step()
    for delta in range(-TOTP_STEP_TOLERANCE, TOTP_STEP_TOLERANCE + 1):
        step = now + delta
        if step <= last_step:
            continue
        candidate = totp.at(step * TOTP_INTERVAL)
        if hmac.compare_digest(candidate, code):
            return step
    return None
