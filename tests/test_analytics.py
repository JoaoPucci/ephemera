"""Tests for app.analytics: registry, validator, record_event, summarize.

The privacy invariant ("payload metadata only, no end-user PII; no user
identity column") is the load-bearing one. Most tests here exercise it
from different angles -- unknown event types, unknown keys, wrong types,
nested containers, oversized strings, out-of-range ints. If a future
refactor accidentally loosens any of those guards, the corresponding
test goes red.

The shipped event type today (`content.limit_hit`) is presence-only
(empty registry schema). The validator-shape tests inject a synthetic
event type with int + bool fields via the `cap_metric_event` fixture --
that decouples the tests from whatever payload schemas EVENT_REGISTRY
happens to ship.
"""

import json
import sqlite3

import pytest

from app import analytics


@pytest.fixture
def cap_metric_event():
    """Inject a synthetic event type with an int + bool field plus an
    associated _INT_FIELD_BOUNDS entry, then clean up. Lets the validator
    tests assert on int/bool/range behavior without tying the assertions
    to whatever payload the shipped events declare today.
    """
    name = "test.cap_metric"
    analytics.EVENT_REGISTRY[name] = {"intended_size_bytes": int, "was_paste": bool}
    analytics._INT_FIELD_BOUNDS["intended_size_bytes"] = (0, 1024 * 1024 * 1024)
    try:
        yield name
    finally:
        del analytics.EVENT_REGISTRY[name]
        del analytics._INT_FIELD_BOUNDS["intended_size_bytes"]


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------


def test_registry_contains_content_limit_hit_as_presence_only():
    """The shipped event type. Registry value is empty `{}` -- presence-only
    semantics: the row's existence is the entire signal. No payload, no
    user identity, no per-event metadata. This is the privacy posture in
    schema form -- a future change that adds keys to this registry entry
    has to defend why."""
    assert analytics.EVENT_REGISTRY["content.limit_hit"] == {}


# ---------------------------------------------------------------------------
# _validate_payload: happy path
# ---------------------------------------------------------------------------


def test_validate_payload_happy_path_round_trips(cap_metric_event):
    payload = {"intended_size_bytes": 150_000, "was_paste": True}
    out = analytics._validate_payload(cap_metric_event, payload)
    assert out == payload


def test_validate_payload_allows_partial_payload(cap_metric_event):
    """Schema keys are opt-in per call site. Absent values are fine."""
    out = analytics._validate_payload(cap_metric_event, {"intended_size_bytes": 50})
    assert out == {"intended_size_bytes": 50}


def test_validate_payload_allows_none_or_empty():
    assert analytics._validate_payload("content.limit_hit", None) == {}
    assert analytics._validate_payload("content.limit_hit", {}) == {}


@pytest.mark.parametrize("falsy_non_dict", [[], "", 0, False, set(), ()])
def test_validate_rejects_falsy_non_dict_payload(falsy_non_dict):
    """Only `None` normalizes to `{}`. Other falsy values that happen to
    not be dicts (`[]`, `''`, `0`, `False`, set(), ()) MUST raise --
    silently coercing them weakens the schema contract and hides bad
    call sites that intended to pass real data but ended up with a
    falsy non-dict."""
    with pytest.raises(
        analytics.AnalyticsValidationError, match="payload must be a dict"
    ):
        analytics._validate_payload("content.limit_hit", falsy_non_dict)


# ---------------------------------------------------------------------------
# _validate_payload: rejection paths (privacy + schema invariants)
# ---------------------------------------------------------------------------


def test_validate_rejects_unknown_event_type():
    with pytest.raises(analytics.AnalyticsValidationError, match="unknown event_type"):
        analytics._validate_payload("not.a.real.event", {})


def test_validate_rejects_unknown_payload_key(cap_metric_event):
    """Per-event-type key allowlist via registry. A typo or an unsanctioned
    field rejects rather than silently landing in the table."""
    with pytest.raises(analytics.AnalyticsValidationError, match="unknown keys"):
        analytics._validate_payload(
            cap_metric_event,
            {"intended_size_bytes": 100, "extra_field": "leaks"},
        )


def test_validate_rejects_unknown_payload_key_on_presence_only_event():
    """A presence-only event (empty registry schema) MUST reject any payload
    keys at all. Otherwise a caller could quietly start writing fields
    that the table contract claims don't exist."""
    with pytest.raises(analytics.AnalyticsValidationError, match="unknown keys"):
        analytics._validate_payload("content.limit_hit", {"intended_size_bytes": 100})


def test_validate_rejects_wrong_type_for_int_field(cap_metric_event):
    with pytest.raises(analytics.AnalyticsValidationError, match="expected int"):
        analytics._validate_payload(cap_metric_event, {"intended_size_bytes": "150000"})


def test_validate_rejects_bool_for_int_field(cap_metric_event):
    """`bool` is a subclass of int in Python; the validator must reject
    a True/False where int is declared so a flag can't masquerade as
    a count."""
    with pytest.raises(analytics.AnalyticsValidationError, match="expected int"):
        analytics._validate_payload(cap_metric_event, {"intended_size_bytes": True})


def test_validate_rejects_wrong_type_for_bool_field(cap_metric_event):
    with pytest.raises(analytics.AnalyticsValidationError, match="expected bool"):
        analytics._validate_payload(cap_metric_event, {"was_paste": 1})


def test_validate_rejects_nested_list(cap_metric_event):
    """The privacy primitive: nested containers can hide a content snippet
    under a flat type-check. Reject structurally."""
    with pytest.raises(analytics.AnalyticsValidationError, match="nested containers"):
        analytics._validate_payload(
            cap_metric_event, {"intended_size_bytes": [1, 2, 3]}
        )


def test_validate_rejects_nested_dict(cap_metric_event):
    with pytest.raises(analytics.AnalyticsValidationError, match="nested containers"):
        analytics._validate_payload(
            cap_metric_event,
            {"intended_size_bytes": {"sneaky": "passphrase"}},
        )


def test_validate_rejects_int_below_lower_bound(cap_metric_event):
    with pytest.raises(analytics.AnalyticsValidationError, match="outside allowed"):
        analytics._validate_payload(cap_metric_event, {"intended_size_bytes": -1})


def test_validate_rejects_int_above_upper_bound(cap_metric_event):
    """Upper bound is 1 GiB to clamp client-asserted sizes."""
    with pytest.raises(analytics.AnalyticsValidationError, match="outside allowed"):
        analytics._validate_payload(
            cap_metric_event,
            {"intended_size_bytes": 1024 * 1024 * 1024 + 1},
        )


# ---------------------------------------------------------------------------
# Privacy: oversized string fixture (the "looks like a passphrase" test)
# ---------------------------------------------------------------------------


def test_validate_rejects_oversized_string_value():
    """The privacy invariant. If a future event type adds a str field, a
    65-char value MUST be rejected -- 64 chars caps enum-style categoricals
    but not free-form strings (passphrases, labels, content snippets).

    Inject a temporary registry entry so the test exercises the str path
    without coupling to whatever happens to be in EVENT_REGISTRY today.
    """
    analytics.EVENT_REGISTRY["test.str_field"] = {"label": str}
    try:
        passphrase_shaped = "correct horse battery staple " * 4  # ~120 chars
        with pytest.raises(
            analytics.AnalyticsValidationError,
            match="MUST NOT enter the analytics payload",
        ):
            analytics._validate_payload("test.str_field", {"label": passphrase_shaped})
        # Sanity: a short (<= 64 char) categorical accepts cleanly.
        out = analytics._validate_payload("test.str_field", {"label": "image_jpeg"})
        assert out == {"label": "image_jpeg"}
    finally:
        del analytics.EVENT_REGISTRY["test.str_field"]


# ---------------------------------------------------------------------------
# record_event: end-to-end against an in-memory DB
# ---------------------------------------------------------------------------


def _make_in_memory_db():
    """Stand up an in-memory SQLite DB with just the analytics_events table.
    Avoids the full init_db() ceremony for unit tests of record_event.
    Mirrors the v5+ shape: no user_id column."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE analytics_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            occurred_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            payload TEXT NOT NULL DEFAULT '{}'
        )
    """)
    return conn


def test_record_event_writes_presence_only_row():
    """Happy path: the shipped event type is presence-only. The row records
    `event_type` + auto `occurred_at`; payload is `{}`. No user identity
    is persisted -- the schema has no user_id column."""
    conn = _make_in_memory_db()
    analytics.record_event(conn, "content.limit_hit")
    rows = conn.execute("SELECT event_type, payload FROM analytics_events").fetchall()
    assert len(rows) == 1
    event_type, payload_json = rows[0]
    assert event_type == "content.limit_hit"
    assert json.loads(payload_json) == {}


def test_record_event_writes_validated_row(cap_metric_event):
    """A non-presence-only event round-trips its payload through the
    validator. Uses the synthetic test event so we exercise int + bool
    fields without coupling to a shipped event's payload shape."""
    conn = _make_in_memory_db()
    analytics.record_event(
        conn,
        cap_metric_event,
        payload={"intended_size_bytes": 250_000, "was_paste": True},
    )
    rows = conn.execute("SELECT event_type, payload FROM analytics_events").fetchall()
    assert len(rows) == 1
    event_type, payload_json = rows[0]
    assert event_type == cap_metric_event
    assert json.loads(payload_json) == {
        "intended_size_bytes": 250_000,
        "was_paste": True,
    }


def test_record_event_propagates_validation_error():
    """A bad call doesn't silently no-op; the writer raises so the call
    site sees the bug at first run."""
    conn = _make_in_memory_db()
    with pytest.raises(analytics.AnalyticsValidationError):
        analytics.record_event(
            conn,
            "content.limit_hit",
            payload={"unknown_key": 1},
        )
    # Nothing was written.
    rows = conn.execute("SELECT COUNT(*) FROM analytics_events").fetchone()
    assert rows[0] == 0


# ---------------------------------------------------------------------------
# summarize: the read path the admin CLI uses
# ---------------------------------------------------------------------------


def test_summarize_zero_events(tmp_db_path):
    out = analytics.summarize("content.limit_hit")
    assert out == {"count": 0, "fields": {}}


def test_summarize_presence_only_event_returns_count(tmp_db_path):
    """For presence-only events (`content.limit_hit`), summarize should
    return only count -- there are no int fields to percentile-aggregate.
    Counts over time are the entire query surface for these events."""
    from app.models._core import _connect

    with _connect() as conn:
        for _ in range(7):
            analytics.record_event(conn, "content.limit_hit")
    out = analytics.summarize("content.limit_hit")
    assert out == {"count": 7, "fields": {}}


def test_summarize_aggregates_int_field_percentiles(tmp_db_path, cap_metric_event):
    """Insert a small distribution and assert the percentile slots work.
    Uses the synthetic test event (with an int field) so we exercise the
    aggregation path without depending on a shipped event having an int
    field. Percentile indexing follows nearest-rank: index =
    max(0, min(n-1, ceil(n*p)-1))."""
    from app.models._core import _connect

    sizes = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]  # n = 10
    with _connect() as conn:
        for sz in sizes:
            analytics.record_event(
                conn,
                cap_metric_event,
                payload={"intended_size_bytes": sz, "was_paste": False},
            )
    out = analytics.summarize(cap_metric_event)
    assert out["count"] == 10
    stats = out["fields"]["intended_size_bytes"]
    assert stats["count"] == 10
    assert stats["min"] == 10
    assert stats["max"] == 100
    # p50: ceil(10*0.5)-1 = 4 -> values[4] = 50
    assert stats["p50"] == 50
    # p95: ceil(10*0.95)-1 = 9 -> values[9] = 100 (max; expected for n=10)
    assert stats["p95"] == 100


def test_summarize_p95_does_not_overshoot_when_np_is_exact_integer(
    tmp_db_path, cap_metric_event
):
    """Regression for the int(n*p) overshoot bug. For n=20 and p=0.95,
    n*p is exactly 19.0; the buggy formula `int(19.0) = 19` returns
    values[19] (the max), biasing capacity-planning telemetry high.
    The correct nearest-rank index is `ceil(19.0) - 1 = 18`, which
    returns values[18] -- the actual 95th-percentile sample."""
    from app.models._core import _connect

    sizes = list(range(1, 21))  # 1..20, n=20
    with _connect() as conn:
        for sz in sizes:
            analytics.record_event(
                conn,
                cap_metric_event,
                payload={"intended_size_bytes": sz, "was_paste": False},
            )
    stats = analytics.summarize(cap_metric_event)["fields"]["intended_size_bytes"]
    assert stats["min"] == 1
    assert stats["max"] == 20
    # p95 must be 19, NOT 20 (the max). The old `int(n*p)` formula
    # returned 20 here -- this assertion locks in the fix.
    assert stats["p95"] == 19, (
        "p95 of [1..20] should be 19 (nearest-rank); reporting 20 means "
        "the int(n*p) overshoot regressed"
    )


def test_summarize_rejects_unknown_event_type(tmp_db_path):
    with pytest.raises(analytics.AnalyticsValidationError):
        analytics.summarize("not.a.real.event")
