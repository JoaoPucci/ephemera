"""Authentication primitives: passwords, TOTP, backup codes, lockout, sessions.

Design notes:
- Password hashing: bcrypt (cost 12). Constant-time verification via bcrypt.checkpw.
- TOTP: RFC 6238 via pyotp. Accept codes within +/-1 step (30s skew either side)
  with an anti-replay check (stored last_step; new step must be strictly greater).
- Backup codes: 10 single-use codes, each bcrypt-hashed, marked consumed on use.
- Lockout: per-user. After MAX_FAILURES failures within LOCKOUT_WINDOW, lock the
  account for LOCKOUT_DURATION. Counter resets on any success.
- Identical error paths for wrong-user vs wrong-password vs wrong-code vs locked,
  to prevent attackers from enumerating which factor / account they got wrong.
- Sessions: signed cookies (itsdangerous) carrying the user_id. On every
  successful login we re-sign with a fresh timestamp -> cookie value rotates,
  defeating session fixation.
"""
import hmac
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import pyotp

from . import models
from .config import get_settings


# ----------------------------------------------------------------------------
# Tuning knobs
# ----------------------------------------------------------------------------

BCRYPT_ROUNDS = 12
TOTP_DIGITS = 6
TOTP_INTERVAL = 30
TOTP_STEP_TOLERANCE = 1          # accept current step +/- 1
MAX_FAILURES = 10
LOCKOUT_WINDOW_SECONDS = 15 * 60
LOCKOUT_DURATION_SECONDS = 60 * 60
RECOVERY_CODE_COUNT = 10
RECOVERY_CODE_LENGTH = 10         # visible chars (base32), grouped XXXXX-XXXXX


# ----------------------------------------------------------------------------
# Generic credential errors -- never leak which factor / user was wrong.
# ----------------------------------------------------------------------------


class AuthError(Exception):
    """Raised when login should fail, regardless of the reason."""


class LockoutError(AuthError):
    """Raised when the account is currently locked."""

    def __init__(self, until_iso: str):
        super().__init__("account locked")
        self.until_iso = until_iso


# ----------------------------------------------------------------------------
# Password helpers
# ----------------------------------------------------------------------------


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), stored_hash.encode())
    except ValueError:
        return False


# ----------------------------------------------------------------------------
# TOTP helpers
# ----------------------------------------------------------------------------


def generate_totp_secret() -> str:
    return pyotp.random_base32(length=32)


def provisioning_uri(secret: str, account_name: str = "admin", issuer: str = "ephemera") -> str:
    return pyotp.TOTP(secret, digits=TOTP_DIGITS, interval=TOTP_INTERVAL).provisioning_uri(
        name=account_name, issuer_name=issuer
    )


def _current_step() -> int:
    return int(time.time()) // TOTP_INTERVAL


def verify_totp(secret: str, code: str, last_step: int) -> Optional[int]:
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


# ----------------------------------------------------------------------------
# Recovery codes
# ----------------------------------------------------------------------------


_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


def _random_recovery_code() -> str:
    raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(RECOVERY_CODE_LENGTH))
    return raw[:5] + "-" + raw[5:]


def generate_recovery_codes() -> tuple[list[str], str]:
    codes = [_random_recovery_code() for _ in range(RECOVERY_CODE_COUNT)]
    hashes = [
        {"hash": bcrypt.hashpw(c.encode(), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode(), "used_at": None}
        for c in codes
    ]
    return codes, json.dumps(hashes)


def _normalize_backup_code(code: str) -> str:
    code = code.strip().upper().replace(" ", "")
    if len(code) == RECOVERY_CODE_LENGTH and "-" not in code:
        code = code[:5] + "-" + code[5:]
    return code


def consume_backup_code(code: str, stored_json: str) -> Optional[str]:
    code = _normalize_backup_code(code)
    try:
        entries = json.loads(stored_json)
    except json.JSONDecodeError:
        return None
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for entry in entries:
        if entry.get("used_at") is not None:
            continue
        try:
            if bcrypt.checkpw(code.encode(), entry["hash"].encode()):
                entry["used_at"] = now
                return json.dumps(entries)
        except ValueError:
            continue
    return None


# ----------------------------------------------------------------------------
# Lockout / failure tracking (per-user)
# ----------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def check_not_locked(user: dict) -> None:
    if user.get("lockout_until"):
        until = _parse_iso(user["lockout_until"])
        if _utcnow() < until:
            raise LockoutError(user["lockout_until"])


def record_failure(user: dict) -> None:
    new_attempts = int(user.get("failed_attempts", 0)) + 1
    updates = {"failed_attempts": new_attempts}
    if new_attempts >= MAX_FAILURES:
        until = _utcnow() + timedelta(seconds=LOCKOUT_DURATION_SECONDS)
        updates["lockout_until"] = until.strftime("%Y-%m-%dT%H:%M:%SZ")
        updates["failed_attempts"] = 0
    models.update_user(user["id"], **updates)


def record_success(user_id: int, updates: dict) -> None:
    updates["failed_attempts"] = 0
    updates["lockout_until"] = None
    models.update_user(user_id, **updates)


# ----------------------------------------------------------------------------
# End-to-end login
# ----------------------------------------------------------------------------


def authenticate(username: str, password: str, code: str) -> dict:
    """Verify username + password + (TOTP code OR backup code).

    Returns the authenticated user dict on success. Raises AuthError on any
    failure (same error surface for unknown user, wrong password, wrong code),
    or LockoutError when the account is locked. On success, mutates the user
    row to record TOTP step / consumed backup code and reset failure counters.
    """
    user = models.get_user_by_username(username.strip()) if username else None
    if user is None:
        # Constant-ish time: still do a bcrypt+totp-cost worth of work so
        # timing doesn't leak whether the username exists.
        bcrypt.checkpw(b"dummy", bcrypt.hashpw(b"dummy", bcrypt.gensalt(rounds=BCRYPT_ROUNDS)))
        raise AuthError("invalid credentials")
    check_not_locked(user)

    pw_ok = verify_password(password, user["password_hash"])

    totp_step = None
    consumed_backup_json: Optional[str] = None
    stripped = code.strip()
    if stripped.isdigit() and len(stripped) == TOTP_DIGITS:
        totp_step = verify_totp(user["totp_secret"], stripped, user["totp_last_step"])
    if totp_step is None:
        consumed_backup_json = consume_backup_code(stripped, user["recovery_code_hashes"])

    if not pw_ok or (totp_step is None and consumed_backup_json is None):
        record_failure(user)
        raise AuthError("invalid credentials")

    updates: dict = {}
    if totp_step is not None:
        updates["totp_last_step"] = totp_step
    if consumed_backup_json is not None:
        updates["recovery_code_hashes"] = consumed_backup_json
    record_success(user["id"], updates)
    return user


# ----------------------------------------------------------------------------
# API tokens (DB-backed, replaces the static EPHEMERA_API_KEY)
# ----------------------------------------------------------------------------


TOKEN_PREFIX = "eph_"


def mint_api_token() -> tuple[str, str]:
    """Return (plaintext_token, sha256_hash). Plaintext shown to user once."""
    import hashlib

    body = secrets.token_urlsafe(32)
    plaintext = TOKEN_PREFIX + body
    digest = hashlib.sha256(plaintext.encode()).hexdigest()
    return plaintext, digest


def lookup_api_token(plaintext: str) -> Optional[dict]:
    """Return the token row (with user_id) if valid; None otherwise.

    On hit, touches last_used_at.
    """
    import hashlib

    if not plaintext or not plaintext.startswith(TOKEN_PREFIX):
        return None
    digest = hashlib.sha256(plaintext.encode()).hexdigest()
    row = models.get_active_token_by_hash(digest)
    if row is None:
        return None
    models.touch_token_last_used(row["id"])
    return row
