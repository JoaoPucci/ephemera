"""End-to-end login orchestration: combines password + TOTP/recovery check,
lockout enforcement, and success-path side effects (step bump, code
consumption, counter reset)."""
from typing import Optional

import bcrypt

from .. import models
from ..security_log import emit as audit
from ._core import BCRYPT_ROUNDS, TOTP_DIGITS, AuthError
from .lockout import check_not_locked, record_failure, record_success
from .password import verify_password
from .recovery_codes import consume_backup_code
from .totp import verify_totp


def authenticate(username: str, password: str, code: str, client_ip: str = "cli") -> dict:
    """Verify username + password + (TOTP code OR backup code).

    Returns the authenticated user dict on success. Raises AuthError on any
    failure (same error surface for unknown user, wrong password, wrong code),
    or LockoutError when the account is locked. On success, mutates the user
    row to record TOTP step / consumed backup code and reset failure counters.

    Emits structured security events for every branch. `client_ip` is taken
    verbatim into the event; pass the request's remote IP for web logins,
    "cli" (the default) for admin re-auth paths.
    """
    trimmed = username.strip() if username else ""
    user = models.get_user_by_username(trimmed) if trimmed else None
    if user is None:
        # Constant-ish time: still do a bcrypt+totp-cost worth of work so
        # timing doesn't leak whether the username exists.
        bcrypt.checkpw(b"dummy", bcrypt.hashpw(b"dummy", bcrypt.gensalt(rounds=BCRYPT_ROUNDS)))
        audit("login.failure", username=trimmed, client_ip=client_ip, reason="unknown_user")
        raise AuthError("invalid credentials")
    try:
        check_not_locked(user)
    except Exception:
        audit(
            "login.failure",
            user_id=user["id"], username=user["username"],
            client_ip=client_ip, reason="locked",
        )
        raise

    pw_ok = verify_password(password, user["password_hash"])

    totp_step = None
    consumed_backup_json: Optional[str] = None
    stripped = code.strip()
    if stripped.isdigit() and len(stripped) == TOTP_DIGITS:
        totp_step = verify_totp(user["totp_secret"], stripped, user["totp_last_step"])
    if totp_step is None:
        consumed_backup_json = consume_backup_code(stripped, user["recovery_code_hashes"])

    if not pw_ok or (totp_step is None and consumed_backup_json is None):
        lockout_until = record_failure(user)
        reason = "wrong_password" if not pw_ok else "wrong_second_factor"
        audit(
            "login.failure",
            user_id=user["id"], username=user["username"],
            client_ip=client_ip, reason=reason,
        )
        if lockout_until is not None:
            audit(
                "login.lockout",
                user_id=user["id"], username=user["username"],
                client_ip=client_ip, until=lockout_until,
            )
        raise AuthError("invalid credentials")

    updates: dict = {}
    if totp_step is not None:
        updates["totp_last_step"] = totp_step
    if consumed_backup_json is not None:
        updates["recovery_code_hashes"] = consumed_backup_json
    record_success(user["id"], updates)
    audit(
        "login.success",
        user_id=user["id"], username=user["username"], client_ip=client_ip,
    )
    return user
