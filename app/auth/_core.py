"""Constants, exception types, and datetime helpers shared by every auth
submodule. Kept underscore-prefixed so the public `app.auth` namespace stays
focused on the per-concern functions."""
from datetime import datetime, timezone


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
# Datetime helpers
# ----------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
