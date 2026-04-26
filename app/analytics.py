"""Lightweight event-based analytics.

Records anonymous metadata events (sizes, counts, durations, anonymous
categorical values) -- never user content (passphrases, secret content,
file bytes, labels) or any end-user PII. The on-disk table is
`analytics_events` (see app/models/_core.py).

Events are append-only; no end-user-facing read API. The admin CLI
(`ephemera-admin analytics-summary <event_type>`) is the only query
path. Single-admin tool, low volume, no GDPR right-to-be-forgotten
obligation today.

# Privacy invariant

Payload values are restricted to bool / int / float / str. Strings cap
at 64 chars -- that fits enum-style categoricals but won't accommodate
a leaked passphrase / label / content snippet. Nested dicts and lists
are rejected structurally so a list/dict can't smuggle one in either.

# Why a registry

Each event type declares its allowed `key -> type` schema in
EVENT_REGISTRY. Calls that pass an unknown event type or a payload that
violates the schema raise `AnalyticsValidationError`. This makes
"someone added a new event type" reviewable in diff (registry mutation
shows up cleanly), rather than "someone passed wrong fields, the row
landed in the table without validation."

# Why no per-event-type rate limit

Today the only writer is `POST /api/secrets`, which is gated by the
existing `create_limiter` (60/hour per session). At that ceiling, a
single admin can produce at most ~525k rows/year of `content.limit_hit`
events -- bounded by the endpoint, not by analytics-side throttling.

When a future event type fires from a non-rate-limited path, that's
the moment to revisit. Don't add throttling on speculation.

# Adding a new event type

1. Add an entry to EVENT_REGISTRY below mapping `event_type -> {key: type}`.
2. If any int field needs a sanity range, add it to _INT_FIELD_BOUNDS.
3. Write a unit test in tests/test_analytics.py asserting the schema
   validates and a sample payload round-trips.
4. Wire the call from the route / module that should emit it.

Anything not following this path can't write to the table -- which is
the structural guarantee that "no end-user PII enters analytics."
"""

import json
import math
import sqlite3
from typing import Any

from .models import _core

# Per-event-type payload schema. Each entry: event type -> {key: type}.
# Keys absent from the schema reject; values not matching the declared
# type reject; nested containers reject. See _validate_payload.
EVENT_REGISTRY: dict[str, dict[str, type]] = {
    "content.limit_hit": {
        "intended_size_bytes": int,
        "was_paste": bool,
    },
}

# Maximum value-cap for str-type payload fields. 64 chars fits enum-style
# categorical labels but not a leaked passphrase / label / content snippet
# -- which is the privacy boundary this code enforces.
_MAX_STRING_VALUE_LEN = 64

# Sanity bounds on common int fields. Range-clamp before storing so a
# client-asserted size can't write a meaningless value.
_INT_FIELD_BOUNDS: dict[str, tuple[int, int]] = {
    "intended_size_bytes": (0, 1024 * 1024 * 1024),  # 0 to 1 GiB
}


class AnalyticsValidationError(ValueError):
    """Raised when a record_event() call doesn't conform to the registry."""


def _validate_payload(event_type: str, payload: dict | None) -> dict:
    """Pure function -- no DB. Validates payload against EVENT_REGISTRY
    and returns the validated copy. Raises AnalyticsValidationError on
    any deviation (unknown event type, unknown keys, wrong types, nested
    containers, oversized strings, out-of-range ints)."""
    if event_type not in EVENT_REGISTRY:
        raise AnalyticsValidationError(
            f"unknown event_type {event_type!r}; not in EVENT_REGISTRY"
        )
    schema = EVENT_REGISTRY[event_type]
    # Only None normalizes to {}. Other falsy non-dicts (`[]`, `''`, `0`,
    # `False`, ...) MUST raise -- silently coercing them via `payload or {}`
    # would weaken the schema contract and hide bad call sites that meant
    # to pass real data but somehow ended up with a falsy non-dict.
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise AnalyticsValidationError(
            f"payload must be a dict, got {type(payload).__name__}"
        )

    extra_keys = set(payload) - set(schema)
    if extra_keys:
        raise AnalyticsValidationError(
            f"event_type {event_type!r} payload has unknown keys: {sorted(extra_keys)}"
        )

    out: dict[str, Any] = {}
    for key, expected_type in schema.items():
        if key not in payload:
            # Absent values are allowed. Presence is opt-in per call site.
            continue
        value = payload[key]
        # Reject nested containers structurally before the type check, so a
        # `list[int]` doesn't slip through "expected int" via duck-typing.
        if isinstance(value, (list, dict, tuple, set)):
            raise AnalyticsValidationError(
                f"event_type {event_type!r} key {key!r}: nested containers "
                f"not allowed (got {type(value).__name__})"
            )
        if expected_type is bool:
            if not isinstance(value, bool):
                raise AnalyticsValidationError(
                    f"event_type {event_type!r} key {key!r}: expected bool, "
                    f"got {type(value).__name__}"
                )
        elif expected_type is int:
            # `bool` is a subclass of int in Python; reject explicit bool
            # where int is declared so True/False can't masquerade as a
            # count. The `isinstance(value, bool)` guard is load-bearing.
            if isinstance(value, bool) or not isinstance(value, int):
                raise AnalyticsValidationError(
                    f"event_type {event_type!r} key {key!r}: expected int, "
                    f"got {type(value).__name__}"
                )
            if key in _INT_FIELD_BOUNDS:
                lo, hi = _INT_FIELD_BOUNDS[key]
                if not (lo <= value <= hi):
                    raise AnalyticsValidationError(
                        f"event_type {event_type!r} key {key!r}: {value} "
                        f"outside allowed range [{lo}, {hi}]"
                    )
        elif expected_type is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise AnalyticsValidationError(
                    f"event_type {event_type!r} key {key!r}: expected number, "
                    f"got {type(value).__name__}"
                )
        elif expected_type is str:
            if not isinstance(value, str):
                raise AnalyticsValidationError(
                    f"event_type {event_type!r} key {key!r}: expected str, "
                    f"got {type(value).__name__}"
                )
            if len(value) > _MAX_STRING_VALUE_LEN:
                raise AnalyticsValidationError(
                    f"event_type {event_type!r} key {key!r}: string length "
                    f"{len(value)} exceeds analytics-payload cap of "
                    f"{_MAX_STRING_VALUE_LEN}. Free-form strings (passphrases, "
                    "labels, content) MUST NOT enter the analytics payload."
                )
        else:
            raise AnalyticsValidationError(
                f"event_type {event_type!r} schema declares unsupported type "
                f"for key {key!r}: {expected_type}"
            )
        out[key] = value

    return out


def record_event(
    conn: sqlite3.Connection,
    event_type: str,
    *,
    payload: dict | None = None,
    user_id: int | None = None,
) -> None:
    """Append a row to analytics_events after validating the payload.

    Privacy invariant: payload contains metadata only -- sizes, counts,
    durations, anonymous categorical values. Never user content.
    Enforced structurally by _validate_payload's type / length / nesting
    checks.

    user_id is intentionally a bare `int`, not a user row -- the
    analytics surface MUST NOT be a vector for opt-in TOTP reads or any
    other side-effect on the user record. ON DELETE SET NULL on the FK
    preserves trend integrity through admin rotation.

    The caller manages the connection (positional first arg) so analytics
    writes can join an existing transaction if the call site has one.
    The route handler that emits content.limit_hit doesn't need
    transaction unification, but other future emitters might.
    """
    validated = _validate_payload(event_type, payload)
    conn.execute(
        "INSERT INTO analytics_events (event_type, user_id, payload) VALUES (?, ?, ?)",
        (event_type, user_id, json.dumps(validated)),
    )


def _percentile_index(n: int, p: float) -> int:
    """Zero-based nearest-rank index for percentile `p` of `n` sorted values,
    clamped to [0, n-1]. Uses `ceil(n*p) - 1` rather than `int(n*p)` so that
    when `n*p` is an exact integer the index doesn't overshoot by one --
    e.g. for n=20 samples and p=0.95, `int(20*0.95)=19` returns the max
    (the last element); `ceil(19)-1=18` correctly returns the 95th-percentile
    sample. This biases high-tail metrics low rather than high, which is
    the right direction for capacity-planning telemetry where
    overestimating the tail leads to over-provisioning.
    """
    return max(0, min(n - 1, math.ceil(n * p) - 1))


def summarize(event_type: str) -> dict[str, Any]:
    """Read-only aggregation over events of a given type. Returns count
    plus, for each int field in the event type's schema, p50/p95/p99 of
    its observed values. Used by `ephemera-admin analytics-summary`.
    """
    if event_type not in EVENT_REGISTRY:
        raise AnalyticsValidationError(
            f"unknown event_type {event_type!r}; not in EVENT_REGISTRY"
        )
    with _core._connect() as conn:
        rows = conn.execute(
            "SELECT payload FROM analytics_events WHERE event_type = ?",
            (event_type,),
        ).fetchall()

    out: dict[str, Any] = {"count": len(rows), "fields": {}}
    if not rows:
        return out

    schema = EVENT_REGISTRY[event_type]
    for field, expected_type in schema.items():
        if expected_type is not int:
            continue
        values = sorted(
            v
            for v in (json.loads(r[0]).get(field) for r in rows)
            if isinstance(v, int) and not isinstance(v, bool)
        )
        if not values:
            continue
        n = len(values)
        out["fields"][field] = {
            "count": n,
            "min": values[0],
            "p50": values[_percentile_index(n, 0.50)],
            "p95": values[_percentile_index(n, 0.95)],
            "p99": values[_percentile_index(n, 0.99)],
            "max": values[-1],
        }
    return out
