"""Tests for app.models: CRUD, expiry queries, tracking behavior, user scoping."""

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app import models


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _mk(user_id: int, **overrides: Any) -> dict[str, Any]:
    """Small helper to keep tests compact."""
    params = dict(
        content_type="text",
        mime_type=None,
        ciphertext=b"c",
        server_key=b"\x01" * 16,
        passphrase_hash=None,
        track=False,
        expires_in=3600,
    )
    params.update(overrides)
    # `params` is built as a generic `dict[str, ...]` for compactness;
    # the per-field types match `create_secret`'s signature at call
    # time but mypy can't narrow through the `**params` unpacking.
    return models.create_secret(user_id=user_id, **params)  # type: ignore[arg-type]


def test_init_db_creates_secrets_users_and_tokens_tables(tmp_db_path: Path) -> None:
    import sqlite3

    with sqlite3.connect(tmp_db_path) as conn:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {"secrets", "users", "api_tokens"} <= names


def test_create_secret_returns_id_token_and_expires_at(provisioned_user: dict[str, Any]) -> None:
    result = _mk(provisioned_user["id"])
    assert "id" in result
    assert "token" in result
    assert "expires_at" in result
    assert len(result["token"]) >= 16


def test_token_is_unique(provisioned_user: dict[str, Any]) -> None:
    tokens = {_mk(provisioned_user["id"])["token"] for _ in range(30)}
    assert len(tokens) == 30


def test_get_by_token_returns_row(provisioned_user: dict[str, Any]) -> None:
    r = _mk(provisioned_user["id"], ciphertext=b"cipher")
    row = models.get_by_token(r["token"])
    assert row is not None
    assert row["id"] == r["id"]
    assert row["ciphertext"] == b"cipher"
    assert row["content_type"] == "text"
    assert row["user_id"] == provisioned_user["id"]


def test_get_by_missing_token_returns_none(tmp_db_path: Path) -> None:
    assert models.get_by_token("does-not-exist") is None


def test_delete_secret_removes_row(provisioned_user: dict[str, Any]) -> None:
    r = _mk(provisioned_user["id"])
    models.delete_secret(r["id"])
    assert models.get_by_token(r["token"]) is None


def test_mark_viewed_on_tracked_nulls_payload_keeps_metadata(provisioned_user: dict[str, Any]) -> None:
    r = _mk(provisioned_user["id"], track=True, passphrase_hash="hash")
    models.mark_viewed(r["id"])
    row = models.get_by_id(r["id"], provisioned_user["id"])
    assert row is not None
    assert row["status"] == "viewed"
    assert row["ciphertext"] is None
    assert row["server_key"] is None
    assert row["passphrase"] is None
    assert row["viewed_at"] is not None


def test_mark_viewed_on_untracked_deletes_row(provisioned_user: dict[str, Any]) -> None:
    r = _mk(provisioned_user["id"])
    models.mark_viewed(r["id"])
    assert models.get_by_id(r["id"], provisioned_user["id"]) is None


def test_consume_for_reveal_first_call_wins_second_loses_untracked(provisioned_user: dict[str, Any]) -> None:
    r = _mk(provisioned_user["id"])
    assert models.consume_for_reveal(r["id"], track=False) is True
    assert models.consume_for_reveal(r["id"], track=False) is False
    assert models.get_by_id(r["id"], provisioned_user["id"]) is None


def test_consume_for_reveal_first_call_wins_second_loses_tracked(provisioned_user: dict[str, Any]) -> None:
    r = _mk(provisioned_user["id"], track=True)
    assert models.consume_for_reveal(r["id"], track=True) is True
    assert models.consume_for_reveal(r["id"], track=True) is False
    row = models.get_by_id(r["id"], provisioned_user["id"])
    assert row is not None
    assert row["status"] == "viewed"
    assert row["ciphertext"] is None
    assert row["server_key"] is None
    assert row["passphrase"] is None
    assert row["viewed_at"] is not None


def test_consume_for_reveal_under_concurrency_exactly_one_winner(provisioned_user: dict[str, Any]) -> None:
    """Two threads racing through consume_for_reveal: exactly one True."""
    import threading

    r = _mk(provisioned_user["id"])
    barrier = threading.Barrier(2)
    results: list[bool] = []
    lock = threading.Lock()

    def attempt() -> None:
        barrier.wait()
        won = models.consume_for_reveal(r["id"], track=False)
        with lock:
            results.append(won)

    t1 = threading.Thread(target=attempt)
    t2 = threading.Thread(target=attempt)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert sorted(results) == [False, True]


def test_get_status_returns_pending_before_view(provisioned_user: dict[str, Any]) -> None:
    r = _mk(provisioned_user["id"], track=True)
    status = models.get_status(r["id"], provisioned_user["id"])
    assert status is not None and status["status"] == "pending"


def test_get_status_returns_viewed_after_reveal_on_tracked(provisioned_user: dict[str, Any]) -> None:
    r = _mk(provisioned_user["id"], track=True)
    models.mark_viewed(r["id"])
    status = models.get_status(r["id"], provisioned_user["id"])
    assert status is not None
    assert status["status"] == "viewed"
    assert status["viewed_at"] is not None


def test_get_status_returns_none_for_untracked(provisioned_user: dict[str, Any]) -> None:
    r = _mk(provisioned_user["id"])
    assert models.get_status(r["id"], provisioned_user["id"]) is None


def test_get_status_returns_none_for_other_users_secret(provisioned_user: dict[str, Any], make_user: Callable[..., dict[str, Any]]) -> None:
    bob = make_user("bob")
    r = _mk(provisioned_user["id"], track=True)
    # Bob cannot read Alice's status.
    assert models.get_status(r["id"], bob["id"]) is None
    # Alice still can.
    assert models.get_status(r["id"], provisioned_user["id"]) is not None


def test_increment_attempts_increments_counter(provisioned_user: dict[str, Any]) -> None:
    r = _mk(provisioned_user["id"], passphrase_hash="hash")
    assert models.increment_attempts(r["id"]) == 1
    assert models.increment_attempts(r["id"]) == 2
    assert models.increment_attempts(r["id"]) == 3


def test_purge_expired_removes_expired_rows(provisioned_user: dict[str, Any]) -> None:
    fresh = _mk(provisioned_user["id"])
    stale = _mk(provisioned_user["id"], expires_in=-60)
    purged = models.purge_expired()
    assert purged >= 1
    assert models.get_by_token(fresh["token"]) is not None
    assert models.get_by_token(stale["token"]) is None


def test_purge_tracked_metadata_after_retention_window(provisioned_user: dict[str, Any]) -> None:
    r = _mk(provisioned_user["id"], track=True)
    models.mark_viewed(r["id"])
    models._force_viewed_at(r["id"], _iso(_utcnow() - timedelta(days=31)))
    purged = models.purge_tracked_metadata(retention_seconds=30 * 86400)
    assert purged == 1
    assert models.get_status(r["id"], provisioned_user["id"]) is None


def test_is_expired_returns_true_after_expires_at(provisioned_user: dict[str, Any]) -> None:
    r = _mk(provisioned_user["id"], expires_in=-1)
    row = models.get_by_token(r["token"])
    assert row is not None
    assert models.is_expired(row) is True


def test_is_expired_returns_false_for_fresh_secret(provisioned_user: dict[str, Any]) -> None:
    r = _mk(provisioned_user["id"])
    row = models.get_by_token(r["token"])
    assert row is not None
    assert models.is_expired(row) is False


# ---------------------------------------------------------------------------
# Multi-user: tracked-list isolation, untrack scoping
# ---------------------------------------------------------------------------


def test_list_tracked_secrets_scopes_by_user(provisioned_user: dict[str, Any], make_user: Callable[..., dict[str, Any]]) -> None:
    bob = make_user("bob")
    _mk(provisioned_user["id"], track=True, label="alice-one")
    _mk(provisioned_user["id"], track=True, label="alice-two")
    _mk(bob["id"], track=True, label="bob-one")

    alice_rows = models.list_tracked_secrets(provisioned_user["id"])
    bob_rows = models.list_tracked_secrets(bob["id"])

    assert {r["label"] for r in alice_rows} == {"alice-one", "alice-two"}
    assert {r["label"] for r in bob_rows} == {"bob-one"}


def test_cancel_tracked_wipes_payload_and_flags_status(provisioned_user: dict[str, Any]) -> None:
    r = _mk(provisioned_user["id"], track=True, passphrase_hash="hash")
    ok = models.cancel(r["id"], provisioned_user["id"])
    assert ok is True
    row = models.get_by_id(r["id"], provisioned_user["id"])
    assert row is not None
    assert row["status"] == "canceled"
    assert row["ciphertext"] is None
    assert row["server_key"] is None
    assert row["passphrase"] is None
    assert row["viewed_at"] is not None


def test_cancel_untracked_deletes_row(provisioned_user: dict[str, Any]) -> None:
    r = _mk(provisioned_user["id"])
    assert models.cancel(r["id"], provisioned_user["id"]) is True
    assert models.get_by_id(r["id"], provisioned_user["id"]) is None


def test_cancel_on_already_viewed_secret_returns_false(provisioned_user: dict[str, Any]) -> None:
    r = _mk(provisioned_user["id"], track=True)
    models.mark_viewed(r["id"])
    assert models.cancel(r["id"], provisioned_user["id"]) is False


def test_cancel_cannot_touch_other_users_secret(provisioned_user: dict[str, Any], make_user: Callable[..., dict[str, Any]]) -> None:
    bob = make_user("bob")
    r = _mk(provisioned_user["id"], track=True)
    assert models.cancel(r["id"], bob["id"]) is False
    row = models.get_by_id(r["id"], provisioned_user["id"])
    assert row is not None and row["ciphertext"] is not None  # untouched


def test_cancel_receiver_url_stops_working(provisioned_user: dict[str, Any]) -> None:
    r = _mk(provisioned_user["id"], track=True)
    token = r["token"]
    before = models.get_by_token(token)
    assert before is not None and before["ciphertext"] is not None
    models.cancel(r["id"], provisioned_user["id"])
    after = models.get_by_token(token)
    assert after is not None and after["ciphertext"] is None


def test_clear_non_pending_tracked_removes_only_non_live(provisioned_user: dict[str, Any]) -> None:
    uid = provisioned_user["id"]
    live = _mk(uid, track=True)  # pending, live
    viewed = _mk(uid, track=True)
    models.mark_viewed(viewed["id"])
    burned = _mk(uid, track=True)
    models.burn(burned["id"])
    canceled = _mk(uid, track=True)
    models.cancel(canceled["id"], uid)
    expired = _mk(uid, track=True, expires_in=-60)

    removed = models.clear_non_pending_tracked(uid)
    assert removed == 4  # viewed + burned + canceled + expired
    rows = models.list_tracked_secrets(uid)
    assert [r["id"] for r in rows] == [live["id"]]


def test_clear_non_pending_tracked_scopes_by_user(provisioned_user: dict[str, Any], make_user: Callable[..., dict[str, Any]]) -> None:
    alice_id = provisioned_user["id"]
    bob = make_user("bob")

    ra = _mk(alice_id, track=True)
    models.mark_viewed(ra["id"])
    rb = _mk(bob["id"], track=True)
    models.mark_viewed(rb["id"])

    models.clear_non_pending_tracked(alice_id)
    # Alice's row is gone; Bob's is still there.
    assert models.list_tracked_secrets(alice_id) == []
    assert len(models.list_tracked_secrets(bob["id"])) == 1


def test_clear_non_pending_tracked_returns_zero_when_nothing_to_clear(provisioned_user: dict[str, Any]) -> None:
    _mk(provisioned_user["id"], track=True)  # only a live one
    assert models.clear_non_pending_tracked(provisioned_user["id"]) == 0


def test_list_tracked_reports_canceled_status(provisioned_user: dict[str, Any]) -> None:
    r = _mk(provisioned_user["id"], track=True)
    models.cancel(r["id"], provisioned_user["id"])
    items = models.list_tracked_secrets(provisioned_user["id"])
    assert len(items) == 1
    assert items[0]["status"] == "canceled"
    assert items[0]["viewed_at"] is not None


def test_untrack_scopes_by_user(provisioned_user: dict[str, Any], make_user: Callable[..., dict[str, Any]]) -> None:
    bob = make_user("bob")
    r = _mk(provisioned_user["id"], track=True)
    # Bob cannot untrack Alice's secret.
    assert models.untrack(r["id"], bob["id"]) is False
    row = models.get_by_id(r["id"], provisioned_user["id"])
    assert row is not None and row["track"] == 1
    # Alice can.
    assert models.untrack(r["id"], provisioned_user["id"]) is True


def test_cascade_on_delete_user_drops_their_secrets_and_tokens(provisioned_user: dict[str, Any]) -> None:
    r = _mk(provisioned_user["id"], track=True)
    from app import auth

    _, digest = auth.mint_api_token()
    models.create_token(user_id=provisioned_user["id"], name="t1", token_hash=digest)

    models.delete_user(provisioned_user["id"])
    assert models.get_by_id(r["id"], provisioned_user["id"]) is None
    assert models.list_tokens(provisioned_user["id"]) == []


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def test_user_count_and_lookup_by_username(tmp_db_path: Path) -> None:
    from app import auth

    assert models.user_count() == 0
    uid = models.create_user(
        username="alice",
        password_hash=auth.hash_password("pw12345678"),
        totp_secret=auth.generate_totp_secret(),
        recovery_code_hashes="[]",
    )
    assert models.user_count() == 1
    u = models.get_user_by_username("alice")
    assert u is not None and u["id"] == uid
    assert models.get_user_by_username("bob") is None


def test_username_is_unique(tmp_db_path: Path) -> None:
    from app import auth

    models.create_user(
        username="alice",
        password_hash=auth.hash_password("pw12345678"),
        totp_secret=auth.generate_totp_secret(),
        recovery_code_hashes="[]",
    )
    with pytest.raises(sqlite3.IntegrityError):
        models.create_user(
            username="alice",
            password_hash=auth.hash_password("pw12345678"),
            totp_secret=auth.generate_totp_secret(),
            recovery_code_hashes="[]",
        )


# ---------------------------------------------------------------------------
# Migration: legacy single-user DB upgrades cleanly
# ---------------------------------------------------------------------------


def test_list_users_returns_every_row(provisioned_user: dict[str, Any], make_user: Callable[..., dict[str, Any]]) -> None:
    """Sanity check on list_users -- covered indirectly by the admin CLI
    list-users command but not exercised at the model layer."""
    make_user("bob")
    make_user("carol")
    rows = models.list_users()
    usernames = {r["username"] for r in rows}
    assert usernames == {provisioned_user["username"], "bob", "carol"}


def test_update_user_with_no_fields_is_a_noop(provisioned_user: dict[str, Any]) -> None:
    """Edge case: an empty kwargs dict shouldn't produce an empty UPDATE
    statement (SQLite would error); the function should return early."""
    before = models.get_user_by_id(provisioned_user["id"])
    models.update_user(provisioned_user["id"])  # no fields at all
    after = models.get_user_by_id(provisioned_user["id"])
    assert before is not None and after is not None
    assert before["updated_at"] == after["updated_at"]  # no-op means no timestamp bump


def test_update_user_rejects_unknown_columns(provisioned_user: dict[str, Any]) -> None:
    """update_user builds the SET clause via f-string interpolation over
    its kwargs (values are parameterised; column names are not). A future
    caller that accidentally threaded user-influenced dict keys through
    this function would turn it into a SQL-injection sink. Guard at the
    boundary with a whitelist so the interpolation only ever reaches
    known-good column names."""
    import pytest

    with pytest.raises(ValueError) as exc:
        models.update_user(
            provisioned_user["id"],
            password_hash="looks-fine",
            injected_column="would-hit-f-string-interpolation",
        )
    assert "injected_column" in str(exc.value)

    # Sanity: the known-good kwarg was never applied because the whole
    # call was rejected before any SQL ran.
    row = models.get_user_by_id(provisioned_user["id"])
    assert row is not None
    assert row["password_hash"] != "looks-fine"


def test_update_user_accepts_every_documented_writable_column(provisioned_user: dict[str, Any]) -> None:
    """The whitelist has to include every column that real callers update.
    Catch the regression where adding a new column to the users schema
    without also naming it in _ALLOWED_UPDATE_COLUMNS would break the
    matching CLI command silently."""
    # Call update_user with each whitelisted column (using benign values
    # where the field has a tight shape). If any current real caller
    # passes a column that's missing from the whitelist, this test fires.
    models.update_user(
        provisioned_user["id"],
        username=provisioned_user["username"],  # unchanged
        email=None,
        password_hash="$2b$12$unusedunusedunusedunusedunusedunusedunusedunusedunusedu",
        totp_last_step=42,
        failed_attempts=0,
        lockout_until=None,
        session_generation=1,
    )


def test_set_analytics_opt_in_returns_new_value_on_actual_change(provisioned_user: dict[str, Any]) -> None:
    """Atomic toggle: when the desired value differs from the current
    one, the SQL UPDATE fires and returns the new value. This is the
    "real change" path the route uses to gate security_log emission."""
    # Default is 0 from the v6 migration.
    persisted = models.set_analytics_opt_in(provisioned_user["id"], 1)
    assert persisted == 1
    fresh = models.get_user_by_id(provisioned_user["id"])
    assert fresh is not None
    assert fresh["analytics_opt_in"] == 1


def test_set_analytics_opt_in_returns_none_when_value_already_matches(provisioned_user: dict[str, Any]) -> None:
    """Concurrency-safe no-op: when the desired value matches what's
    already in the row, the conditional WHERE clause skips the UPDATE
    and RETURNING produces no row. The route uses None as the signal
    "don't emit security_log, don't claim a flip happened."

    Pre-fix this was a Python-side `if desired != current` check that
    races against concurrent PATCHes (two requests both observing the
    same pre-flip value can no-op a real change). Putting the
    comparison in SQL keeps it atomic; this test pins that behavior."""
    # Already 0 by default. Ask for 0 -> no-op.
    persisted = models.set_analytics_opt_in(provisioned_user["id"], 0)
    assert persisted is None
    fresh = models.get_user_by_id(provisioned_user["id"])
    assert fresh is not None
    assert fresh["analytics_opt_in"] == 0


def test_set_analytics_opt_in_returns_none_for_unknown_user(tmp_db_path: Path) -> None:
    """Defensive: a stale or hand-crafted call against a nonexistent
    user_id must not silently succeed. RETURNING on a zero-row UPDATE
    yields no row; the function reports None and the caller knows
    nothing changed."""
    persisted = models.set_analytics_opt_in(999_999, 1)
    assert persisted is None


def test_default_user_getters_do_not_return_totp_secret(provisioned_user: dict[str, Any]) -> None:
    """The default user-row accessors must never hand back the TOTP
    plaintext. Most call sites (session auth, bearer auth, admin flows
    that aren't TOTP-facing) have no business seeing the seed; the two
    that do use the explicit `_with_totp` variant. Keeping the default
    key-less means a future log line or error handler that dumps a user
    dict can't leak it."""
    by_id = models.get_user_by_id(provisioned_user["id"])
    by_name = models.get_user_by_username(provisioned_user["username"])

    assert by_id is not None and by_name is not None
    assert "totp_secret" not in by_id
    assert "totp_secret" not in by_name
    # Opt-in variant still works for the callers that need it.
    with_totp = models.get_user_with_totp_by_id(provisioned_user["id"])
    assert with_totp is not None
    assert with_totp["totp_secret"] == provisioned_user["totp_secret"]
    with_totp_by_name = models.get_user_with_totp_by_username(
        provisioned_user["username"]
    )
    assert with_totp_by_name is not None
    assert with_totp_by_name["totp_secret"] == provisioned_user["totp_secret"]


def test_fresh_db_is_stamped_to_current_schema_version(tmp_db_path: Path) -> None:
    """init_db on a fresh DB must leave schema_version at CURRENT_SCHEMA_VERSION;
    a later boot can then compare and refuse downgrade."""
    import sqlite3

    from app.models._core import CURRENT_SCHEMA_VERSION

    with sqlite3.connect(tmp_db_path) as conn:
        row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
    assert row is not None
    assert int(row[0]) == CURRENT_SCHEMA_VERSION


def test_init_db_is_idempotent_across_reruns(tmp_db_path: Path) -> None:
    """Running init_db a second time must not regress the version or duplicate
    the single schema_version row (the CHECK constraint would reject)."""
    import sqlite3

    from app.models._core import CURRENT_SCHEMA_VERSION

    models.init_db()  # second run
    with sqlite3.connect(tmp_db_path) as conn:
        rows = conn.execute("SELECT id, version FROM schema_version").fetchall()
    assert len(rows) == 1
    assert int(rows[0][1]) == CURRENT_SCHEMA_VERSION


def test_init_db_refuses_to_run_against_newer_schema(tmp_db_path: Path) -> None:
    """Operator rolled the code back onto a DB that a newer build already
    migrated. We'd rather fail loudly than quietly query with an assumed-
    older column layout."""
    import sqlite3

    import pytest

    from app.models._core import CURRENT_SCHEMA_VERSION, SchemaVersionError

    with sqlite3.connect(tmp_db_path) as conn:
        conn.execute(
            "UPDATE schema_version SET version = ? WHERE id = 1",
            (CURRENT_SCHEMA_VERSION + 1,),
        )
        conn.commit()
    with pytest.raises(SchemaVersionError):
        models.init_db()


def test_legacy_db_migrates_to_multiuser_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A DB written by the pre-multi-user schema should gain username on users
    and user_id on secrets/api_tokens when init_db() runs over it."""
    import sqlite3

    legacy_db = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy_db)
    conn.executescript("""
        CREATE TABLE secrets (
            id TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL,
            server_key BLOB, ciphertext BLOB, content_type TEXT NOT NULL,
            mime_type TEXT, passphrase TEXT, track INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            label TEXT, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, viewed_at TEXT);
        CREATE TABLE users (
            id INTEGER PRIMARY KEY, password_hash TEXT NOT NULL, totp_secret TEXT NOT NULL,
            totp_last_step INTEGER NOT NULL DEFAULT 0,
            recovery_code_hashes TEXT NOT NULL DEFAULT '[]',
            failed_attempts INTEGER NOT NULL DEFAULT 0, lockout_until TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE api_tokens (
            id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, token_hash TEXT NOT NULL,
            created_at TEXT NOT NULL, last_used_at TEXT, revoked_at TEXT);
        INSERT INTO users (id, password_hash, totp_secret, created_at, updated_at)
            VALUES (1, 'hash', 'secret', '2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z');
        INSERT INTO secrets (id, token, server_key, ciphertext, content_type,
                             created_at, expires_at)
            VALUES ('legacy-sid', 'legacy-tok', X'0102', X'0304', 'text',
                    '2025-01-01T00:00:00Z', '2099-01-01T00:00:00Z');
        INSERT INTO api_tokens (name, token_hash, created_at)
            VALUES ('legacy-tok-name', 'deadbeef', '2025-01-01T00:00:00Z');
    """)
    conn.commit()
    conn.close()

    monkeypatch.setenv("EPHEMERA_DB_PATH", str(legacy_db))
    monkeypatch.setenv("EPHEMERA_SECRET_KEY", "k")
    from app import config

    config.get_settings.cache_clear()

    models.init_db()

    # After migration: columns exist, legacy rows backfilled to user_id=1,
    # username set to 'admin'.
    u = models.get_user_by_username("admin")
    assert u is not None and u["id"] == 1
    sec = models.get_by_id("legacy-sid", 1)
    assert sec is not None and sec["user_id"] == 1
    toks = models.list_tokens(1)
    assert len(toks) == 1 and toks[0]["name"] == "legacy-tok-name"

    # Legacy DB had no schema_version table. The upgrade must stamp it to
    # CURRENT so subsequent boots compare against a known value.
    import sqlite3

    from app.models._core import CURRENT_SCHEMA_VERSION

    with sqlite3.connect(legacy_db) as conn:
        (stamped,) = conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()
    assert int(stamped) == CURRENT_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Schema v3 -- CHECK constraints on user-controlled TEXT columns
# ---------------------------------------------------------------------------


def test_v3_check_constraints_present_on_fresh_db(tmp_db_path: Path) -> None:
    """Fresh DBs land at v3 directly via TABLES_SCRIPT, which now embeds the
    CHECK clauses inline. The migration's table-rebuild path is for legacy
    v2 DBs and is exercised in test_v2_legacy_db_clean_upgrades_to_v3 below.
    """
    with sqlite3.connect(str(tmp_db_path)) as conn:
        secrets_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='secrets'"
        ).fetchone()[0]
        users_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
        ).fetchone()[0]
    assert "length(passphrase) <= 80" in secrets_sql
    assert "length(label) <= 60" in secrets_sql
    assert "length(username) <= 256" in users_sql


def test_v3_check_rejects_oversized_passphrase(tmp_db_path: Path) -> None:
    with sqlite3.connect(str(tmp_db_path)) as conn:
        # Insert a user first so the FK has a target.
        conn.execute(
            "INSERT INTO users (username, password_hash, totp_secret, "
            "created_at, updated_at) VALUES ('u', 'h', 's', 'now', 'now')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO secrets (id, user_id, token, content_type, "
                "passphrase, created_at, expires_at) "
                "VALUES ('s1', 1, 't1', 'text', ?, 'now', 'later')",
                ("X" * 81,),
            )


def test_v3_check_rejects_oversized_label(tmp_db_path: Path) -> None:
    with sqlite3.connect(str(tmp_db_path)) as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, totp_secret, "
            "created_at, updated_at) VALUES ('u', 'h', 's', 'now', 'now')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO secrets (id, user_id, token, content_type, "
                "label, created_at, expires_at) "
                "VALUES ('s2', 1, 't2', 'text', ?, 'now', 'later')",
                ("X" * 61,),
            )


def test_v3_check_rejects_oversized_username(tmp_db_path: Path) -> None:
    with (
        sqlite3.connect(str(tmp_db_path)) as conn,
        pytest.raises(sqlite3.IntegrityError),
    ):
        conn.execute(
            "INSERT INTO users (username, password_hash, totp_secret, "
            "created_at, updated_at) VALUES (?, 'h', 's', 'now', 'now')",
            ("u" * 257,),
        )


def test_v3_check_allows_null_optional_columns(tmp_db_path: Path) -> None:
    """passphrase and label are nullable; the CHECK clauses must not fire
    on NULL values (the IS NULL OR length(...) form preserves nullability)."""
    with sqlite3.connect(str(tmp_db_path)) as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, totp_secret, "
            "created_at, updated_at) VALUES ('u', 'h', 's', 'now', 'now')"
        )
        # Both passphrase and label NULL: must not raise.
        conn.execute(
            "INSERT INTO secrets (id, user_id, token, content_type, "
            "created_at, expires_at) "
            "VALUES ('s3', 1, 't3', 'text', 'now', 'later')"
        )


def _seed_v2_db(db_path: Path) -> None:
    """Hand-roll a v2 DB shape (tables WITHOUT CHECK clauses, schema_version
    stamped to 2). Used by the v2->v3 migration tests below."""
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE users (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                username              TEXT NOT NULL,
                email                 TEXT,
                password_hash         TEXT NOT NULL,
                totp_secret           TEXT NOT NULL,
                totp_last_step        INTEGER NOT NULL DEFAULT 0,
                recovery_code_hashes  TEXT NOT NULL DEFAULT '[]',
                failed_attempts       INTEGER NOT NULL DEFAULT 0,
                lockout_until         TEXT,
                session_generation    INTEGER NOT NULL DEFAULT 0,
                preferred_language    TEXT,
                created_at            TEXT NOT NULL,
                updated_at            TEXT NOT NULL
            );
            CREATE TABLE secrets (
                id            TEXT PRIMARY KEY,
                user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token         TEXT UNIQUE NOT NULL,
                server_key    BLOB,
                ciphertext    BLOB,
                content_type  TEXT NOT NULL,
                mime_type     TEXT,
                passphrase    TEXT,
                track         INTEGER NOT NULL DEFAULT 0,
                status        TEXT NOT NULL DEFAULT 'pending',
                attempts      INTEGER NOT NULL DEFAULT 0,
                label         TEXT,
                created_at    TEXT NOT NULL,
                expires_at    TEXT NOT NULL,
                viewed_at     TEXT
            );
            CREATE TABLE api_tokens (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name          TEXT NOT NULL,
                token_hash    TEXT NOT NULL,
                created_at    TEXT NOT NULL,
                last_used_at  TEXT,
                revoked_at    TEXT
            );
            CREATE TABLE schema_version (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL
            );
            INSERT INTO schema_version (id, version) VALUES (1, 2);
            """
        )


def test_v2_legacy_db_clean_upgrades_to_v3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A v2 DB with no rows that violate the new CHECKs upgrades through to
    v3. After migration: tables carry the CHECK clauses, schema_version is
    stamped to 3, indices are present, FKs are intact."""
    db = tmp_path / "v2.db"
    _seed_v2_db(db)

    monkeypatch.setenv("EPHEMERA_DB_PATH", str(db))
    monkeypatch.setenv("EPHEMERA_SECRET_KEY", "test-secret-key-abcdef0123456789")
    from app import config

    config.get_settings.cache_clear()
    try:
        models.init_db()
        with sqlite3.connect(str(db)) as conn:
            (ver,) = conn.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()
            secrets_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='secrets'"
            ).fetchone()[0]
            users_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
            ).fetchone()[0]
            # Indices got rebuilt by INDICES_SCRIPT after the migration.
            idx_names = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
        from app.models._core import CURRENT_SCHEMA_VERSION

        # init_db() runs all registered migrations to land at the current
        # version; assert the v3 work in particular survived the chain
        # (CHECK clauses present, indices rebuilt). The version-stamp
        # assertion uses CURRENT_SCHEMA_VERSION so future v5+ migrations
        # don't require touching this test.
        assert ver == CURRENT_SCHEMA_VERSION
        assert "CHECK" in secrets_sql
        assert "CHECK" in users_sql
        assert "idx_secrets_token" in idx_names
        assert "idx_users_username" in idx_names
    finally:
        config.get_settings.cache_clear()


def test_v3_migration_preserves_users_autoincrement_no_reuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression guard: the four-step table swap MUST preserve sqlite_sequence
    for the users table so AUTOINCREMENT keeps its no-reuse guarantee. Without
    the explicit restore, the post-migration counter collapses to MAX(id) of
    surviving rows, and a deleted user's id can be reissued to a new signup.
    Session cookies in this codebase are keyed by user_id+session_generation,
    so id reuse is a real cookie-replay risk -- not just a hygiene concern."""
    db = tmp_path / "v2_seq.db"
    _seed_v2_db(db)
    with sqlite3.connect(str(db)) as conn:
        # Insert three users with sequential ids, then delete the highest.
        # Pre-migration sqlite_sequence tracks 3 (the historical max), even
        # though only ids 1 and 2 remain in the users table.
        for username in ["u1", "u2", "u3"]:
            conn.execute(
                "INSERT INTO users (username, password_hash, totp_secret, "
                "created_at, updated_at) VALUES (?, 'h', 's', 'now', 'now')",
                (username,),
            )
        conn.execute("DELETE FROM users WHERE username = 'u3'")
        seq = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='users'"
        ).fetchone()
        assert seq is not None and seq[0] == 3, (
            "fixture sanity: sqlite_sequence should track 3 after delete"
        )

    monkeypatch.setenv("EPHEMERA_DB_PATH", str(db))
    monkeypatch.setenv("EPHEMERA_SECRET_KEY", "test-secret-key-abcdef0123456789")
    from app import config

    config.get_settings.cache_clear()
    try:
        models.init_db()
        with sqlite3.connect(str(db)) as conn:
            seq = conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='users'"
            ).fetchone()
            # Insert a new user via AUTOINCREMENT (omit id). It must get
            # id=4, NOT id=3 (the deleted user's id).
            conn.execute(
                "INSERT INTO users (username, password_hash, totp_secret, "
                "created_at, updated_at) "
                "VALUES ('u4', 'h', 's', 'now', 'now')"
            )
            (new_id,) = conn.execute(
                "SELECT id FROM users WHERE username = 'u4'"
            ).fetchone()
        assert seq is not None and seq[0] >= 3, (
            f"sqlite_sequence collapsed to {seq[0] if seq else None}; "
            "AUTOINCREMENT no-reuse guarantee was lost across the migration"
        )
        assert new_id == 4, (
            f"new user got id {new_id}, expected 4 (id 3 belonged to a "
            "deleted user and must not be reused)"
        )
    finally:
        config.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# v3 pre-flight violation guards: a v2 DB containing rows that would violate
# the new CHECK constraints must be refused with a remediable SchemaVersionError
# (column + count) rather than crash mid-swap. These are pre-flight aborts;
# the table rebuild never runs, so post-test the DB is still at v2.
# ---------------------------------------------------------------------------


def _run_init_db_against(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Boilerplate-cutter for the v3 violation tests below: point settings at
    the supplied path and call init_db, returning whatever it raises (or None
    on success)."""
    from app import config

    monkeypatch.setenv("EPHEMERA_DB_PATH", str(db))
    monkeypatch.setenv("EPHEMERA_SECRET_KEY", "test-secret-key-abcdef0123456789")
    config.get_settings.cache_clear()
    try:
        models.init_db()
    finally:
        config.get_settings.cache_clear()


def test_v3_migration_aborts_when_passphrase_exceeds_check_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "v2_passphrase.db"
    _seed_v2_db(db)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, totp_secret, "
            "created_at, updated_at) "
            "VALUES ('u1', 'h', 's', 'now', 'now')"
        )
        # 81-char passphrase: just over the new <=80 cap.
        conn.execute(
            "INSERT INTO secrets (id, user_id, token, content_type, "
            "passphrase, created_at, expires_at) "
            "VALUES ('s1', 1, 't1', 'text', ?, 'now', 'later')",
            ("p" * 81,),
        )

    from app.models._core import SchemaVersionError

    with pytest.raises(SchemaVersionError) as exc:
        _run_init_db_against(db, monkeypatch)
    msg = str(exc.value)
    assert "secrets.passphrase" in msg
    assert "1 row(s) exceed 80 chars" in msg


def test_v3_migration_aborts_when_label_exceeds_check_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "v2_label.db"
    _seed_v2_db(db)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, totp_secret, "
            "created_at, updated_at) "
            "VALUES ('u1', 'h', 's', 'now', 'now')"
        )
        # 61-char label: just over the new <=60 cap.
        conn.execute(
            "INSERT INTO secrets (id, user_id, token, content_type, "
            "label, created_at, expires_at) "
            "VALUES ('s1', 1, 't1', 'text', ?, 'now', 'later')",
            ("L" * 61,),
        )

    from app.models._core import SchemaVersionError

    with pytest.raises(SchemaVersionError) as exc:
        _run_init_db_against(db, monkeypatch)
    msg = str(exc.value)
    assert "secrets.label" in msg
    assert "1 row(s) exceed 60 chars" in msg


def test_v3_migration_aborts_when_username_exceeds_check_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "v2_username.db"
    _seed_v2_db(db)
    with sqlite3.connect(str(db)) as conn:
        # 257-char username: just over the new <=256 cap.
        conn.execute(
            "INSERT INTO users (username, password_hash, totp_secret, "
            "created_at, updated_at) "
            "VALUES (?, 'h', 's', 'now', 'now')",
            ("u" * 257,),
        )

    from app.models._core import SchemaVersionError

    with pytest.raises(SchemaVersionError) as exc:
        _run_init_db_against(db, monkeypatch)
    msg = str(exc.value)
    assert "users.username" in msg
    assert "1 row(s) exceed 256 chars" in msg


def test_v3_migration_aborts_with_all_violations_in_one_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Multiple violations across columns are all reported together so an
    operator can fix them in one pass instead of replaying init_db three times."""
    db = tmp_path / "v2_all.db"
    _seed_v2_db(db)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, totp_secret, "
            "created_at, updated_at) VALUES (?, 'h', 's', 'now', 'now')",
            ("u" * 257,),
        )
        conn.execute(
            "INSERT INTO secrets (id, user_id, token, content_type, "
            "passphrase, label, created_at, expires_at) "
            "VALUES ('s1', 1, 't1', 'text', ?, ?, 'now', 'later')",
            ("p" * 81, "L" * 61),
        )

    from app.models._core import SchemaVersionError

    with pytest.raises(SchemaVersionError) as exc:
        _run_init_db_against(db, monkeypatch)
    msg = str(exc.value)
    assert "secrets.passphrase" in msg
    assert "secrets.label" in msg
    assert "users.username" in msg


# Two more defensive branches in v3 (sqlite_sequence-table-absent fallback +
# the post-swap UPDATE-then-INSERT fallback) are pragma'd in v3.py rather
# than tested here. Driving them requires either constructing a v0 legacy
# DB without AUTOINCREMENT anywhere (SQLite refuses to DROP sqlite_sequence,
# so it has to be never-created in the fixture) or relying on undocumented
# swap-time sqlite_sequence semantics that vary across SQLite versions. The
# behaviour they protect (AUTOINCREMENT no-reuse guarantee preservation) is
# already pinned at the test level by
# test_v3_migration_preserves_users_autoincrement_no_reuse, which exercises
# the common path with the same end-state assertion.


# ---------------------------------------------------------------------------
# Schema v4 -- analytics_events table
# ---------------------------------------------------------------------------


def test_analytics_events_table_present_on_fresh_db(tmp_db_path: Path) -> None:
    """Fresh DBs land at the current version directly via TABLES_SCRIPT.
    Schema is in v5 shape: no user_id column (aggregate-only), no FK
    child-column index."""
    with sqlite3.connect(str(tmp_db_path)) as conn:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        idx_names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
    assert "analytics_events" in names
    assert "idx_analytics_events_type_time" in idx_names
    # No user_id column, so no FK child-column index either.
    assert "idx_analytics_events_user_id" not in idx_names


def test_analytics_events_columns_match_v5_design(tmp_db_path: Path) -> None:
    """v5 dropped `user_id` (aggregate-only by design). The table now
    carries only the bare minimum: id, event_type, occurred_at, payload."""
    with sqlite3.connect(str(tmp_db_path)) as conn:
        cols = {
            r[1]: r[2]  # name -> type
            for r in conn.execute("PRAGMA table_info(analytics_events)").fetchall()
        }
    assert cols == {
        "id": "INTEGER",
        "event_type": "TEXT",
        "occurred_at": "TIMESTAMP",
        "payload": "TEXT",
    }
    assert "user_id" not in cols


def test_v3_legacy_db_upgrades_to_current(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Seed a v3 DB (CHECK clauses present, no analytics_events table,
    schema_version stamped at 3), boot the current code, and confirm
    migrations walk it forward through v4 (creates analytics_events with
    user_id) and v5 (drops user_id) without disturbing the v3 CHECK
    constraints."""
    db = tmp_path / "v3.db"
    with sqlite3.connect(str(db)) as conn:
        conn.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL CHECK (length(username) <= 256),
                email TEXT,
                password_hash TEXT NOT NULL,
                totp_secret TEXT NOT NULL,
                totp_last_step INTEGER NOT NULL DEFAULT 0,
                recovery_code_hashes TEXT NOT NULL DEFAULT '[]',
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                lockout_until TEXT,
                session_generation INTEGER NOT NULL DEFAULT 0,
                preferred_language TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE secrets (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token TEXT UNIQUE NOT NULL,
                server_key BLOB,
                ciphertext BLOB,
                content_type TEXT NOT NULL,
                mime_type TEXT,
                passphrase TEXT CHECK (passphrase IS NULL OR length(passphrase) <= 80),
                track INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                label TEXT CHECK (label IS NULL OR length(label) <= 60),
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                viewed_at TEXT
            );
            CREATE TABLE api_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT,
                revoked_at TEXT
            );
            CREATE TABLE schema_version (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL
            );
            INSERT INTO schema_version (id, version) VALUES (1, 3);
            """
        )

    monkeypatch.setenv("EPHEMERA_DB_PATH", str(db))
    monkeypatch.setenv("EPHEMERA_SECRET_KEY", "test-secret-key-abcdef0123456789")
    from app import config

    config.get_settings.cache_clear()
    try:
        models.init_db()
        with sqlite3.connect(str(db)) as conn:
            (ver,) = conn.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()
            names = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            secrets_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='secrets'"
            ).fetchone()[0]
            idx_names = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
        from app.models._core import CURRENT_SCHEMA_VERSION

        assert ver == CURRENT_SCHEMA_VERSION
        assert "analytics_events" in names
        assert "idx_analytics_events_type_time" in idx_names
        # v5 dropped the user_id column + its index; the post-migration DB
        # has no trace of either, regardless of the v4 intermediate state.
        assert "idx_analytics_events_user_id" not in idx_names
        with sqlite3.connect(str(db)) as conn2:
            cols = {
                r[1]
                for r in conn2.execute("PRAGMA table_info(analytics_events)").fetchall()
            }
        assert "user_id" not in cols
        # v3 CHECK clauses survive: v4/v5 migrations don't touch
        # the secrets/users tables.
        assert "CHECK" in secrets_sql
    finally:
        config.get_settings.cache_clear()


def test_v5_migration_resumes_after_rename_interrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Crash recovery: if a previous boot got killed between the v5 migration's
    RENAME and the version stamp, the next boot finds:
        * `_analytics_events_v4` (the renamed real data, never copied)
        * `analytics_events` (recreated empty by TABLES_SCRIPT this boot)
    Without recovery logic, _migrate_to_v5 would re-run, the second RENAME
    would fail because the temp table already exists, and boot would block.

    Seed exactly that state (v4 DB whose analytics_events has been renamed
    away, plus a fresh-empty v5 analytics_events from TABLES_SCRIPT), drop
    the version stamp back to 4 to simulate the un-stamped crash, and
    confirm init_db walks it forward to v5 with the data preserved.
    """
    db = tmp_path / "v5_interrupted.db"
    # Seed a v4-shape DB with one analytics row.
    with sqlite3.connect(str(db)) as conn:
        conn.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL CHECK (length(username) <= 256),
                email TEXT,
                password_hash TEXT NOT NULL,
                totp_secret TEXT NOT NULL,
                totp_last_step INTEGER NOT NULL DEFAULT 0,
                recovery_code_hashes TEXT NOT NULL DEFAULT '[]',
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                lockout_until TEXT,
                session_generation INTEGER NOT NULL DEFAULT 0,
                preferred_language TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE secrets (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token TEXT UNIQUE NOT NULL,
                server_key BLOB,
                ciphertext BLOB,
                content_type TEXT NOT NULL,
                mime_type TEXT,
                passphrase TEXT CHECK (passphrase IS NULL OR length(passphrase) <= 80),
                track INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                label TEXT CHECK (label IS NULL OR length(label) <= 60),
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                viewed_at TEXT
            );
            CREATE TABLE api_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT,
                revoked_at TEXT
            );
            CREATE TABLE analytics_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                occurred_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                payload TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE schema_version (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL
            );
            INSERT INTO schema_version (id, version) VALUES (1, 4);
            """
        )
        # One real row in the v4-shape table -- the data we must not lose.
        conn.execute(
            "INSERT INTO analytics_events (event_type, payload) "
            "VALUES ('content.limit_hit', '{}')"
        )
        # Simulate the prior interrupted run: the RENAME succeeded but
        # nothing after it ran. Schema_version is still stamped at 4.
        conn.execute("ALTER TABLE analytics_events RENAME TO _analytics_events_v4")

    monkeypatch.setenv("EPHEMERA_DB_PATH", str(db))
    monkeypatch.setenv("EPHEMERA_SECRET_KEY", "test-secret-key-abcdef0123456789")
    from app import config

    config.get_settings.cache_clear()
    try:
        # init_db should run TABLES_SCRIPT (creates a fresh empty v5
        # analytics_events because none exists at the canonical name),
        # then re-run _migrate_to_v5 which detects the leftover temp
        # table and resumes from it without dropping the real data.
        models.init_db()
        with sqlite3.connect(str(db)) as conn:
            (ver,) = conn.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()
            cols = {
                r[1]
                for r in conn.execute("PRAGMA table_info(analytics_events)").fetchall()
            }
            (count,) = conn.execute("SELECT COUNT(*) FROM analytics_events").fetchone()
            (event_type,) = conn.execute(
                "SELECT event_type FROM analytics_events"
            ).fetchone()
            temp_still_present = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='_analytics_events_v4'"
            ).fetchone()
        from app.models._core import CURRENT_SCHEMA_VERSION

        assert ver == CURRENT_SCHEMA_VERSION
        assert "user_id" not in cols  # v5 shape, no user identity column
        assert count == 1, "the row that was in flight must be preserved"
        assert event_type == "content.limit_hit"
        assert temp_still_present is None, "temp table must be cleaned up"
    finally:
        config.get_settings.cache_clear()


def test_v6_migration_adds_analytics_opt_in_with_default_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Seed a v5-shape DB with a row that lacks `analytics_opt_in` (legacy
    v5 was the last version where the column didn't exist). Boot the
    current code and confirm v6 added the column, defaulted existing rows
    to 0 (consent-first), and stamped schema_version to current. Calling
    init_db a second time is a no-op (idempotency)."""
    db = tmp_path / "v5.db"
    with sqlite3.connect(str(db)) as conn:
        conn.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL CHECK (length(username) <= 256),
                email TEXT,
                password_hash TEXT NOT NULL,
                totp_secret TEXT NOT NULL,
                totp_last_step INTEGER NOT NULL DEFAULT 0,
                recovery_code_hashes TEXT NOT NULL DEFAULT '[]',
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                lockout_until TEXT,
                session_generation INTEGER NOT NULL DEFAULT 0,
                preferred_language TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE secrets (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token TEXT UNIQUE NOT NULL,
                server_key BLOB,
                ciphertext BLOB,
                content_type TEXT NOT NULL,
                mime_type TEXT,
                passphrase TEXT CHECK (passphrase IS NULL OR length(passphrase) <= 80),
                track INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                label TEXT CHECK (label IS NULL OR length(label) <= 60),
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                viewed_at TEXT
            );
            CREATE TABLE api_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT,
                revoked_at TEXT
            );
            CREATE TABLE analytics_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                occurred_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                payload TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE schema_version (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL
            );
            INSERT INTO schema_version (id, version) VALUES (1, 5);
            INSERT INTO users (
                username, password_hash, totp_secret, created_at, updated_at
            ) VALUES ('alice', 'pw', 'v1:dummy', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
            """
        )

    monkeypatch.setenv("EPHEMERA_DB_PATH", str(db))
    monkeypatch.setenv("EPHEMERA_SECRET_KEY", "test-secret-key-abcdef0123456789")

    from app import config, models
    from app.models._core import CURRENT_SCHEMA_VERSION

    config.get_settings.cache_clear()
    try:
        models.init_db()

        with sqlite3.connect(str(db)) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
            (opt_in,) = conn.execute(
                "SELECT analytics_opt_in FROM users WHERE username = 'alice'"
            ).fetchone()
            (ver,) = conn.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()
        assert "analytics_opt_in" in cols
        assert opt_in == 0  # consent-first default for legacy rows
        assert ver == CURRENT_SCHEMA_VERSION

        # Idempotency: a second init_db must be a no-op (the migration's
        # introspection guard skips the ALTER if the column already exists).
        models.init_db()
        with sqlite3.connect(str(db)) as conn:
            (ver_after,) = conn.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()
        assert ver_after == CURRENT_SCHEMA_VERSION
    finally:
        config.get_settings.cache_clear()


def test_v2_legacy_db_with_violating_rows_aborts_v3_migration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A v2 DB with a row that exceeds a new CHECK ceiling must abort the
    migration with a remediable error message rather than failing mid-INSERT
    inside the table-rebuild step."""
    from app.models._core import SchemaVersionError

    db = tmp_path / "v2_dirty.db"
    _seed_v2_db(db)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, totp_secret, "
            "created_at, updated_at) VALUES (?, 'h', 's', 'now', 'now')",
            ("u" * 300,),  # 300 > 256 -> would violate v3 username CHECK
        )

    monkeypatch.setenv("EPHEMERA_DB_PATH", str(db))
    monkeypatch.setenv("EPHEMERA_SECRET_KEY", "test-secret-key-abcdef0123456789")
    from app import config

    config.get_settings.cache_clear()
    try:
        with pytest.raises(SchemaVersionError) as excinfo:
            models.init_db()
        msg = str(excinfo.value)
        assert "users.username" in msg
        assert "256" in msg
        # Verify we did not partially apply: schema_version is still 2.
        with sqlite3.connect(str(db)) as conn:
            (ver,) = conn.execute(
                "SELECT version FROM schema_version WHERE id = 1"
            ).fetchone()
        assert ver == 2
    finally:
        config.get_settings.cache_clear()

    config.get_settings.cache_clear()
