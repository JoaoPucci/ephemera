"""Constants, exception types, and datetime helpers shared by every auth
submodule. Kept underscore-prefixed so the public `app.auth` namespace stays
focused on the per-concern functions."""

import sys
from datetime import UTC, datetime

# ----------------------------------------------------------------------------
# Tuning knobs
# ----------------------------------------------------------------------------

BCRYPT_ROUNDS = 12

# Test-mode override: when pytest itself is loaded into the process,
# drop the bcrypt cost from 12 (~250ms/hash on CI) to 4 (~1ms/hash).
# Cosmic-ray's per-mutant pytest invocation runs the full ~10min cost-12
# suite once per mutant, which timed out the GitHub-hosted runner's 6h
# ceiling on the weekly mutation run. The behavioural tests assert
# constant-time properties via `monkeypatch`-counted `bcrypt.checkpw`
# calls (not wall-clock measurements), so cost has no effect on the
# security signal -- only on wall-clock duration.
#
# Production safety: `pytest` is in requirements-dev.txt, NOT
# requirements.txt, so a production install from the runtime lockfile
# can't satisfy `import pytest`. `"pytest" in sys.modules` is True only
# when something actively loaded pytest -- in practice, the pytest
# entrypoint itself. The cost-12 source constant above is what
# `test_security_constants_are_not_silently_weakened` (in
# tests/test_fitness_functions.py) AST-pins, so any source-level
# regression of the production cost still trips the fitness gate.
# `pragma: no branch` because the False branch is structurally
# unreachable from inside pytest -- exercising it would require a
# test process where pytest hasn't been loaded, which is paradoxical.
# Production coverage of the False branch is implicit (every prod
# import enters it; no pytest in sys.modules).
if "pytest" in sys.modules:  # pragma: no branch
    BCRYPT_ROUNDS = 4
TOTP_DIGITS = 6
TOTP_INTERVAL = 30
TOTP_STEP_TOLERANCE = 1  # accept current step +/- 1
# Cumulative-since-last-success counter; a successful login resets it to 0.
# There is NO rolling-window decay -- 10 failures spread over a month still
# trip the lockout. Acceptable at this scale because the rescue path (admin
# CLI `reset-password` or login with a recovery code) is short, and the
# alternative adds a `last_failure_at` column without meaningful security
# benefit at a handful of users.
MAX_FAILURES = 10
LOCKOUT_DURATION_SECONDS = 60 * 60
RECOVERY_CODE_COUNT = 10
RECOVERY_CODE_LENGTH = 10  # visible chars (base32), grouped XXXXX-XXXXX


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
    return datetime.now(UTC)


def _parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
