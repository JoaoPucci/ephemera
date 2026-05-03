"""Per-user failure tracking + lockout. After MAX_FAILURES wrong attempts,
the account is locked for LOCKOUT_DURATION_SECONDS; any success resets."""

from datetime import timedelta
from typing import Any

from .. import models
from ._core import (
    LOCKOUT_DURATION_SECONDS,
    MAX_FAILURES,
    LockoutError,
    _parse_iso,
    _utcnow,
)


def check_not_locked(user: dict[str, Any]) -> None:
    if user.get("lockout_until"):
        until = _parse_iso(user["lockout_until"])
        if _utcnow() < until:
            raise LockoutError(user["lockout_until"])


def record_failure(user: dict[str, Any]) -> str | None:
    """Tick the failure counter; if it crosses MAX_FAILURES, lock the account
    and return the ISO `lockout_until` string. Returns None otherwise.

    The caller uses the return value to emit a structured lockout event; the
    DB state change happens either way."""
    new_attempts = int(user.get("failed_attempts", 0)) + 1
    # `failed_attempts` is `int`, `lockout_until` is `str | None`. The
    # explicit annotation keeps mypy from inferring the narrower
    # `dict[str, int]` from the initial literal and then choking on the
    # later `str | None` assignment.
    updates: dict[str, int | str | None] = {"failed_attempts": new_attempts}
    lockout_until: str | None = None
    if new_attempts >= MAX_FAILURES:
        until = _utcnow() + timedelta(seconds=LOCKOUT_DURATION_SECONDS)
        lockout_until = until.strftime("%Y-%m-%dT%H:%M:%SZ")
        updates["lockout_until"] = lockout_until
        updates["failed_attempts"] = 0
    models.update_user(user["id"], **updates)
    return lockout_until


def record_success(user_id: int, updates: dict[str, Any]) -> None:
    updates["failed_attempts"] = 0
    updates["lockout_until"] = None
    models.update_user(user_id, **updates)
