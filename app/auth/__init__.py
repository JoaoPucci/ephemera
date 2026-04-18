"""Authentication primitives: passwords, TOTP, backup codes, lockout, API tokens.

Organized as submodules by concern:

    app.auth._core             -- constants, AuthError/LockoutError
    app.auth.password          -- bcrypt hash + verify
    app.auth.totp              -- RFC 6238 TOTP with +/-1 step + anti-replay
    app.auth.recovery_codes    -- one-time backup codes
    app.auth.lockout           -- per-user failure counter + lockout
    app.auth.tokens            -- DB-backed API tokens (eph_...)
    app.auth.login             -- end-to-end authenticate()

Design notes (retained from the pre-split single file):
- Password hashing: bcrypt (cost 12). Constant-time verification via bcrypt.checkpw.
- TOTP: RFC 6238 via pyotp. Accept codes within +/-1 step (30s skew either side)
  with an anti-replay check (stored last_step; new step must be strictly greater).
- Backup codes: 10 single-use codes, each bcrypt-hashed, marked consumed on use.
- Lockout: per-user. After MAX_FAILURES failures, lock the account for
  LOCKOUT_DURATION. Counter resets on any success.
- Identical error paths for wrong-user vs wrong-password vs wrong-code vs locked,
  to prevent attackers from enumerating which factor / account they got wrong.
- Sessions: signed cookies (itsdangerous) carrying the user_id. On every
  successful login we re-sign with a fresh timestamp -> cookie value rotates,
  defeating session fixation.

This `__init__` re-exports the full public surface so existing
`from app import auth; auth.foo()` call-sites continue to work.
"""
from ._core import (
    BCRYPT_ROUNDS,
    LOCKOUT_DURATION_SECONDS,
    LOCKOUT_WINDOW_SECONDS,
    MAX_FAILURES,
    RECOVERY_CODE_COUNT,
    RECOVERY_CODE_LENGTH,
    TOTP_DIGITS,
    TOTP_INTERVAL,
    TOTP_STEP_TOLERANCE,
    AuthError,
    LockoutError,
)
from .lockout import check_not_locked, record_failure, record_success
from .login import authenticate
from .password import hash_password, verify_password
from .recovery_codes import consume_backup_code, generate_recovery_codes
from .tokens import TOKEN_PREFIX, lookup_api_token, mint_api_token
from .totp import generate_totp_secret, provisioning_uri, verify_totp

__all__ = [
    # constants
    "BCRYPT_ROUNDS",
    "LOCKOUT_DURATION_SECONDS",
    "LOCKOUT_WINDOW_SECONDS",
    "MAX_FAILURES",
    "RECOVERY_CODE_COUNT",
    "RECOVERY_CODE_LENGTH",
    "TOKEN_PREFIX",
    "TOTP_DIGITS",
    "TOTP_INTERVAL",
    "TOTP_STEP_TOLERANCE",
    # errors
    "AuthError",
    "LockoutError",
    # functions
    "authenticate",
    "check_not_locked",
    "consume_backup_code",
    "generate_recovery_codes",
    "generate_totp_secret",
    "hash_password",
    "lookup_api_token",
    "mint_api_token",
    "provisioning_uri",
    "record_failure",
    "record_success",
    "verify_password",
    "verify_totp",
]
