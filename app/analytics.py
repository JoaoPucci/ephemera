"""Lightweight event-based analytics.

Records anonymous metadata events -- counts, presence-only flags, and
optional anonymous categorical/numeric metadata. Never user content
(passphrases, secret content, file bytes, labels) and never user
identity (no `user_id` column, no IP, no session token). Audit-trail
signals belong in security_log.py; this module is for aggregate
product metrics only. The on-disk table is `analytics_events` (see
app/models/_core.py).

# Two-gate emit model

Emissions are gated by BOTH:
  1. Operator: `settings.analytics_enabled` (env `EPHEMERA_ANALYTICS_ENABLED`,
     default false). Instance-level kill switch.
  2. User: `user["analytics_opt_in"]`. Per-account consent. Default false.
The gate is checked inside `record_event*`, not at the call site -- a future
emitter that forgets the gate is a class of bug we want the audit-internal
contract to make impossible. Call sites pass the authenticated user; the
sentinel `NO_USER` documents "this caller has no user context" and refuses
the emit (system events have no consent path today).

The opt-in is checked at emit time but never stored on the row -- the
row remains presence-only, structurally un-joinable to identity.

Events are append-only; no end-user-facing read API. The admin CLI
(`ephemera-admin analytics-summary <event_type>`) is the only query
path. Single-admin tool, low volume, no GDPR right-to-be-forgotten
obligation today.

# Privacy invariant

Payload values are restricted to bool / int / float / str. Strings cap
at 64 chars -- that fits enum-style categoricals but won't accommodate
a leaked passphrase / label / content snippet. Nested dicts and lists
are rejected structurally so a list/dict can't smuggle one in either.
The table itself carries no user_id column: a row says "this event type
happened at time T", nothing about WHO. That's structural, not policy:
even if a future caller wants to attach a user, the schema refuses.

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

from .config import get_settings
from .models import _core

# Sentinel for callers that genuinely have no user context (admin CLI,
# future system event, bg job). Distinct from `None` so the absence is
# intentional in source rather than a forgotten kwarg. Reaching the
# emit path with this sentinel still refuses -- presence-only events
# need a user-consent context to count toward an aggregate -- but the
# sentinel makes the design choice grep-able.
NO_USER = object()

# Per-event-type payload schema. Each entry: event type -> {key: type}.
# An empty {} means "presence-only": the event has no payload, the row's
# existence is the entire signal. Use this whenever the metric you'd act
# on is "did X happen, how often" -- it's a stricter privacy posture
# (nothing to leak in the payload) and aggregate counts are all you'd
# query the table for anyway.
#
# Keys absent from the schema reject; values not matching the declared
# type reject; nested containers reject. See _validate_payload.
#
# IMPORTANT (presence-only invariant): every entry in this registry MUST
# have an empty schema. The user-facing copy at `settings.analytics_help`
# in app/static/i18n/en.json reads "Counts events like 'someone hit the
# message length cap' -- never your messages, links, or identity." That
# promise is honest only as long as no event carries a payload that could
# correlate with a user under aggregation. A future event that genuinely
# needs a payload should ship with its own per-feature opt-in, not relax
# this invariant. tests/test_analytics.py guards the invariant with an
# assertion over the registry.
EVENT_REGISTRY: dict[str, dict[str, type]] = {
    "content.limit_hit": {},  # presence-only: count(*) is the signal
}

# Module-level marker for the presence-only invariant above. The constant
# itself isn't read at runtime -- it exists so a future contributor who
# wants to add a payload field has to delete this line and the matching
# test in tests/test_analytics.py, which surfaces the decision in diff.
_PRESENCE_ONLY_INVARIANT = True

# Maximum value-cap for str-type payload fields. 64 chars fits enum-style
# categorical labels but not a leaked passphrase / label / content snippet
# -- which is the privacy boundary this code enforces.
_MAX_STRING_VALUE_LEN = 64

# Sanity bounds on common int fields. Range-clamp before storing so a
# client-asserted value can't write a meaningless one. Empty today; future
# event types with int fields populate this on a per-field basis.
_INT_FIELD_BOUNDS: dict[str, tuple[int, int]] = {}


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


def _gate(user: object) -> bool:
    """Return True iff both gates pass for this `user` arg. Single-source
    so route call sites don't carry the two-gate logic (a future emitter
    can't forget a check that's structurally part of the recording API).
    See module docstring for the operator/user gate semantics.
    """
    if user is NO_USER:
        return False
    if not isinstance(user, dict):
        raise TypeError(
            "record_event*: user must be a dict (authenticated user row) "
            f"or analytics.NO_USER sentinel; got {type(user).__name__}"
        )
    if not user.get("analytics_opt_in"):
        return False
    return get_settings().analytics_enabled


def record_event(
    conn: sqlite3.Connection,
    event_type: str,
    *,
    user: object,
    payload: dict | None = None,
) -> None:
    """Append a row to analytics_events after validating the payload AND
    confirming both gates (operator env + per-user opt-in) are open. If
    either gate is closed the call is a silent no-op -- callers can emit
    unconditionally, the gating is the analytics module's job.

    Privacy invariant: rows carry no user identity (no user_id, no IP, no
    session token), and payloads are metadata-only -- sizes, counts,
    durations, anonymous categorical values, never user content. Both
    are enforced structurally: the schema has no user_id column, and
    _validate_payload's type/length/nesting checks reject anything that
    would smuggle content in via the payload.

    The caller manages the connection (positional first arg) so analytics
    writes can join an existing transaction if the call site has one.
    Today's only emitter is fire-and-forget post-create, but other
    future emitters might want unification.
    """
    if not _gate(user):
        return
    validated = _validate_payload(event_type, payload)
    conn.execute(
        "INSERT INTO analytics_events (event_type, payload) VALUES (?, ?)",
        (event_type, json.dumps(validated)),
    )


def record_event_standalone(
    event_type: str,
    *,
    user: object,
    payload: dict | None = None,
) -> None:
    """Convenience wrapper that opens a fresh connection, records one event,
    commits, and closes. Use this when the call site has no transaction of
    its own and doesn't need atomicity with surrounding writes -- e.g., the
    sender route's post-create `content.limit_hit` emitter. Use record_event()
    with an explicit conn when joining an existing transaction.

    Same gating + privacy invariants as record_event.
    """
    if not _gate(user):
        return
    with _core._connect() as conn:
        record_event(conn, event_type, user=user, payload=payload)


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
