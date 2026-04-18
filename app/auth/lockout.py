"""Per-user failure tracking + lockout. After MAX_FAILURES wrong attempts,
the account is locked for LOCKOUT_DURATION_SECONDS; any success resets."""
from datetime import timedelta

from .. import models
from ._core import (
    LOCKOUT_DURATION_SECONDS,
    LockoutError,
    MAX_FAILURES,
    _parse_iso,
    _utcnow,
)


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
