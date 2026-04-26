"""Tests for app.analytics: registry, validator, record_event, summarize.

The privacy invariant ("payload metadata only, no end-user PII") is the
load-bearing one. Most tests here exercise it from different angles --
unknown event types, unknown keys, wrong types, nested containers,
oversized strings, out-of-range ints. If a future refactor accidentally
loosens any of those guards, the corresponding test goes red.
"""

import json
import sqlite3

import pytest

from app import analytics

# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------


def test_registry_contains_content_limit_hit():
    """First registered event type. PR 3 will write to it from the
    create-secret route handler."""
    assert "content.limit_hit" in analytics.EVENT_REGISTRY
    schema = analytics.EVENT_REGISTRY["content.limit_hit"]
    assert schema == {"intended_size_bytes": int, "was_paste": bool}


# ---------------------------------------------------------------------------
# _validate_payload: happy path
# ---------------------------------------------------------------------------


def test_validate_payload_happy_path_round_trips():
    payload = {"intended_size_bytes": 150_000, "was_paste": True}
    out = analytics._validate_payload("content.limit_hit", payload)
    assert out == payload


def test_validate_payload_allows_partial_payload():
    """Schema keys are opt-in per call site. Absent values are fine."""
    out = analytics._validate_payload("content.limit_hit", {"intended_size_bytes": 50})
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


def test_validate_rejects_unknown_payload_key():
    """Per-event-type key allowlist via registry. A typo or an unsanctioned
    field rejects rather than silently landing in the table."""
    with pytest.raises(analytics.AnalyticsValidationError, match="unknown keys"):
        analytics._validate_payload(
            "content.limit_hit",
            {"intended_size_bytes": 100, "extra_field": "leaks"},
        )


def test_validate_rejects_wrong_type_for_int_field():
    with pytest.raises(analytics.AnalyticsValidationError, match="expected int"):
        analytics._validate_payload(
            "content.limit_hit", {"intended_size_bytes": "150000"}
        )


def test_validate_rejects_bool_for_int_field():
    """`bool` is a subclass of int in Python; the validator must reject
    a True/False where int is declared so a flag can't masquerade as
    a count."""
    with pytest.raises(analytics.AnalyticsValidationError, match="expected int"):
        analytics._validate_payload("content.limit_hit", {"intended_size_bytes": True})


def test_validate_rejects_wrong_type_for_bool_field():
    with pytest.raises(analytics.AnalyticsValidationError, match="expected bool"):
        analytics._validate_payload("content.limit_hit", {"was_paste": 1})


def test_validate_rejects_nested_list():
    """The privacy primitive: nested containers can hide a content snippet
    under a flat type-check. Reject structurally."""
    with pytest.raises(analytics.AnalyticsValidationError, match="nested containers"):
        analytics._validate_payload(
            "content.limit_hit", {"intended_size_bytes": [1, 2, 3]}
        )


def test_validate_rejects_nested_dict():
    with pytest.raises(analytics.AnalyticsValidationError, match="nested containers"):
        analytics._validate_payload(
            "content.limit_hit",
            {"intended_size_bytes": {"sneaky": "passphrase"}},
        )


def test_validate_rejects_int_below_lower_bound():
    with pytest.raises(analytics.AnalyticsValidationError, match="outside allowed"):
        analytics._validate_payload("content.limit_hit", {"intended_size_bytes": -1})


def test_validate_rejects_int_above_upper_bound():
    """Upper bound is 1 GiB to clamp client-asserted sizes."""
    with pytest.raises(analytics.AnalyticsValidationError, match="outside allowed"):
        analytics._validate_payload(
            "content.limit_hit",
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
    Avoids the full init_db() ceremony for unit tests of record_event."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE analytics_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            occurred_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            user_id INTEGER,
            payload TEXT NOT NULL DEFAULT '{}'
        )
    """)
    return conn


def test_record_event_writes_validated_row():
    conn = _make_in_memory_db()
    analytics.record_event(
        conn,
        "content.limit_hit",
        payload={"intended_size_bytes": 250_000, "was_paste": True},
        user_id=1,
    )
    rows = conn.execute(
        "SELECT event_type, user_id, payload FROM analytics_events"
    ).fetchall()
    assert len(rows) == 1
    event_type, user_id, payload_json = rows[0]
    assert event_type == "content.limit_hit"
    assert user_id == 1
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


def test_record_event_accepts_null_user_id():
    """ON DELETE SET NULL on the FK preserves rows with anonymised user_id.
    record_event should accept user_id=None directly (e.g. for events
    fired pre-auth or from a system path)."""
    conn = _make_in_memory_db()
    analytics.record_event(
        conn,
        "content.limit_hit",
        payload={"intended_size_bytes": 100, "was_paste": False},
        user_id=None,
    )
    (uid,) = conn.execute("SELECT user_id FROM analytics_events").fetchone()
    assert uid is None


# ---------------------------------------------------------------------------
# summarize: the read path the admin CLI uses
# ---------------------------------------------------------------------------


def test_summarize_zero_events(tmp_db_path):
    out = analytics.summarize("content.limit_hit")
    assert out == {"count": 0, "fields": {}}


def test_summarize_aggregates_int_field_percentiles(tmp_db_path):
    """Insert a small distribution and assert the percentile slots work.
    Uses the live tmp_db_path so summarize's `_connect()` resolves to
    the test DB. Percentile indexing follows nearest-rank: index =
    max(0, min(n-1, ceil(n*p)-1))."""
    from app.models._core import _connect

    sizes = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]  # n = 10
    with _connect() as conn:
        for sz in sizes:
            analytics.record_event(
                conn,
                "content.limit_hit",
                payload={"intended_size_bytes": sz, "was_paste": False},
            )
    out = analytics.summarize("content.limit_hit")
    assert out["count"] == 10
    stats = out["fields"]["intended_size_bytes"]
    assert stats["count"] == 10
    assert stats["min"] == 10
    assert stats["max"] == 100
    # p50: ceil(10*0.5)-1 = 4 -> values[4] = 50
    assert stats["p50"] == 50
    # p95: ceil(10*0.95)-1 = 9 -> values[9] = 100 (max; expected for n=10)
    assert stats["p95"] == 100


def test_summarize_p95_does_not_overshoot_when_np_is_exact_integer(tmp_db_path):
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
                "content.limit_hit",
                payload={"intended_size_bytes": sz, "was_paste": False},
            )
    stats = analytics.summarize("content.limit_hit")["fields"]["intended_size_bytes"]
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
