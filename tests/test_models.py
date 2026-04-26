"""Tests for app.models: CRUD, expiry queries, tracking behavior, user scoping."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app import models


def _utcnow():
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _mk(user_id: int, **overrides) -> dict:
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
    return models.create_secret(user_id=user_id, **params)


def test_init_db_creates_secrets_users_and_tokens_tables(tmp_db_path):
    import sqlite3

    with sqlite3.connect(tmp_db_path) as conn:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {"secrets", "users", "api_tokens"} <= names


def test_create_secret_returns_id_token_and_expires_at(provisioned_user):
    result = _mk(provisioned_user["id"])
    assert "id" in result
    assert "token" in result
    assert "expires_at" in result
    assert len(result["token"]) >= 16


def test_token_is_unique(provisioned_user):
    tokens = {_mk(provisioned_user["id"])["token"] for _ in range(30)}
    assert len(tokens) == 30


def test_get_by_token_returns_row(provisioned_user):
    r = _mk(provisioned_user["id"], ciphertext=b"cipher")
    row = models.get_by_token(r["token"])
    assert row is not None
    assert row["id"] == r["id"]
    assert row["ciphertext"] == b"cipher"
    assert row["content_type"] == "text"
    assert row["user_id"] == provisioned_user["id"]


def test_get_by_missing_token_returns_none(tmp_db_path):
    assert models.get_by_token("does-not-exist") is None


def test_delete_secret_removes_row(provisioned_user):
    r = _mk(provisioned_user["id"])
    models.delete_secret(r["id"])
    assert models.get_by_token(r["token"]) is None


def test_mark_viewed_on_tracked_nulls_payload_keeps_metadata(provisioned_user):
    r = _mk(provisioned_user["id"], track=True, passphrase_hash="hash")
    models.mark_viewed(r["id"])
    row = models.get_by_id(r["id"], provisioned_user["id"])
    assert row is not None
    assert row["status"] == "viewed"
    assert row["ciphertext"] is None
    assert row["server_key"] is None
    assert row["passphrase"] is None
    assert row["viewed_at"] is not None


def test_mark_viewed_on_untracked_deletes_row(provisioned_user):
    r = _mk(provisioned_user["id"])
    models.mark_viewed(r["id"])
    assert models.get_by_id(r["id"], provisioned_user["id"]) is None


def test_consume_for_reveal_first_call_wins_second_loses_untracked(provisioned_user):
    r = _mk(provisioned_user["id"])
    assert models.consume_for_reveal(r["id"], track=False) is True
    assert models.consume_for_reveal(r["id"], track=False) is False
    assert models.get_by_id(r["id"], provisioned_user["id"]) is None


def test_consume_for_reveal_first_call_wins_second_loses_tracked(provisioned_user):
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


def test_consume_for_reveal_under_concurrency_exactly_one_winner(provisioned_user):
    """Two threads racing through consume_for_reveal: exactly one True."""
    import threading

    r = _mk(provisioned_user["id"])
    barrier = threading.Barrier(2)
    results: list[bool] = []
    lock = threading.Lock()

    def attempt():
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


def test_get_status_returns_pending_before_view(provisioned_user):
    r = _mk(provisioned_user["id"], track=True)
    status = models.get_status(r["id"], provisioned_user["id"])
    assert status is not None and status["status"] == "pending"


def test_get_status_returns_viewed_after_reveal_on_tracked(provisioned_user):
    r = _mk(provisioned_user["id"], track=True)
    models.mark_viewed(r["id"])
    status = models.get_status(r["id"], provisioned_user["id"])
    assert status is not None
    assert status["status"] == "viewed"
    assert status["viewed_at"] is not None


def test_get_status_returns_none_for_untracked(provisioned_user):
    r = _mk(provisioned_user["id"])
    assert models.get_status(r["id"], provisioned_user["id"]) is None


def test_get_status_returns_none_for_other_users_secret(provisioned_user, make_user):
    bob = make_user("bob")
    r = _mk(provisioned_user["id"], track=True)
    # Bob cannot read Alice's status.
    assert models.get_status(r["id"], bob["id"]) is None
    # Alice still can.
    assert models.get_status(r["id"], provisioned_user["id"]) is not None


def test_increment_attempts_increments_counter(provisioned_user):
    r = _mk(provisioned_user["id"], passphrase_hash="hash")
    assert models.increment_attempts(r["id"]) == 1
    assert models.increment_attempts(r["id"]) == 2
    assert models.increment_attempts(r["id"]) == 3


def test_purge_expired_removes_expired_rows(provisioned_user):
    fresh = _mk(provisioned_user["id"])
    stale = _mk(provisioned_user["id"], expires_in=-60)
    purged = models.purge_expired()
    assert purged >= 1
    assert models.get_by_token(fresh["token"]) is not None
    assert models.get_by_token(stale["token"]) is None


def test_purge_tracked_metadata_after_retention_window(provisioned_user):
    r = _mk(provisioned_user["id"], track=True)
    models.mark_viewed(r["id"])
    models._force_viewed_at(r["id"], _iso(_utcnow() - timedelta(days=31)))
    purged = models.purge_tracked_metadata(retention_seconds=30 * 86400)
    assert purged == 1
    assert models.get_status(r["id"], provisioned_user["id"]) is None


def test_is_expired_returns_true_after_expires_at(provisioned_user):
    r = _mk(provisioned_user["id"], expires_in=-1)
    assert models.is_expired(models.get_by_token(r["token"])) is True


def test_is_expired_returns_false_for_fresh_secret(provisioned_user):
    r = _mk(provisioned_user["id"])
    assert models.is_expired(models.get_by_token(r["token"])) is False


# ---------------------------------------------------------------------------
# Multi-user: tracked-list isolation, untrack scoping
# ---------------------------------------------------------------------------


def test_list_tracked_secrets_scopes_by_user(provisioned_user, make_user):
    bob = make_user("bob")
    _mk(provisioned_user["id"], track=True, label="alice-one")
    _mk(provisioned_user["id"], track=True, label="alice-two")
    _mk(bob["id"], track=True, label="bob-one")

    alice_rows = models.list_tracked_secrets(provisioned_user["id"])
    bob_rows = models.list_tracked_secrets(bob["id"])

    assert {r["label"] for r in alice_rows} == {"alice-one", "alice-two"}
    assert {r["label"] for r in bob_rows} == {"bob-one"}


def test_cancel_tracked_wipes_payload_and_flags_status(provisioned_user):
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


def test_cancel_untracked_deletes_row(provisioned_user):
    r = _mk(provisioned_user["id"])
    assert models.cancel(r["id"], provisioned_user["id"]) is True
    assert models.get_by_id(r["id"], provisioned_user["id"]) is None


def test_cancel_on_already_viewed_secret_returns_false(provisioned_user):
    r = _mk(provisioned_user["id"], track=True)
    models.mark_viewed(r["id"])
    assert models.cancel(r["id"], provisioned_user["id"]) is False


def test_cancel_cannot_touch_other_users_secret(provisioned_user, make_user):
    bob = make_user("bob")
    r = _mk(provisioned_user["id"], track=True)
    assert models.cancel(r["id"], bob["id"]) is False
    row = models.get_by_id(r["id"], provisioned_user["id"])
    assert row is not None and row["ciphertext"] is not None  # untouched


def test_cancel_receiver_url_stops_working(provisioned_user):
    r = _mk(provisioned_user["id"], track=True)
    token = r["token"]
    assert models.get_by_token(token)["ciphertext"] is not None
    models.cancel(r["id"], provisioned_user["id"])
    assert models.get_by_token(token)["ciphertext"] is None


def test_clear_non_pending_tracked_removes_only_non_live(provisioned_user):
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


def test_clear_non_pending_tracked_scopes_by_user(provisioned_user, make_user):
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


def test_clear_non_pending_tracked_returns_zero_when_nothing_to_clear(provisioned_user):
    _mk(provisioned_user["id"], track=True)  # only a live one
    assert models.clear_non_pending_tracked(provisioned_user["id"]) == 0


def test_list_tracked_reports_canceled_status(provisioned_user):
    r = _mk(provisioned_user["id"], track=True)
    models.cancel(r["id"], provisioned_user["id"])
    items = models.list_tracked_secrets(provisioned_user["id"])
    assert len(items) == 1
    assert items[0]["status"] == "canceled"
    assert items[0]["viewed_at"] is not None


def test_untrack_scopes_by_user(provisioned_user, make_user):
    bob = make_user("bob")
    r = _mk(provisioned_user["id"], track=True)
    # Bob cannot untrack Alice's secret.
    assert models.untrack(r["id"], bob["id"]) is False
    row = models.get_by_id(r["id"], provisioned_user["id"])
    assert row is not None and row["track"] == 1
    # Alice can.
    assert models.untrack(r["id"], provisioned_user["id"]) is True


def test_cascade_on_delete_user_drops_their_secrets_and_tokens(provisioned_user):
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


def test_user_count_and_lookup_by_username(tmp_db_path):
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


def test_username_is_unique(tmp_db_path):
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


def test_list_users_returns_every_row(provisioned_user, make_user):
    """Sanity check on list_users -- covered indirectly by the admin CLI
    list-users command but not exercised at the model layer."""
    make_user("bob")
    make_user("carol")
    rows = models.list_users()
    usernames = {r["username"] for r in rows}
    assert usernames == {provisioned_user["username"], "bob", "carol"}


def test_update_user_with_no_fields_is_a_noop(provisioned_user):
    """Edge case: an empty kwargs dict shouldn't produce an empty UPDATE
    statement (SQLite would error); the function should return early."""
    before = models.get_user_by_id(provisioned_user["id"])
    models.update_user(provisioned_user["id"])  # no fields at all
    after = models.get_user_by_id(provisioned_user["id"])
    assert before["updated_at"] == after["updated_at"]  # no-op means no timestamp bump


def test_update_user_rejects_unknown_columns(provisioned_user):
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
    assert row["password_hash"] != "looks-fine"


def test_update_user_accepts_every_documented_writable_column(provisioned_user):
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


def test_default_user_getters_do_not_return_totp_secret(provisioned_user):
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
    assert with_totp["totp_secret"] == provisioned_user["totp_secret"]
    with_totp_by_name = models.get_user_with_totp_by_username(
        provisioned_user["username"]
    )
    assert with_totp_by_name["totp_secret"] == provisioned_user["totp_secret"]


def test_fresh_db_is_stamped_to_current_schema_version(tmp_db_path):
    """init_db on a fresh DB must leave schema_version at CURRENT_SCHEMA_VERSION;
    a later boot can then compare and refuse downgrade."""
    import sqlite3

    from app.models._core import CURRENT_SCHEMA_VERSION

    with sqlite3.connect(tmp_db_path) as conn:
        row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
    assert row is not None
    assert int(row[0]) == CURRENT_SCHEMA_VERSION


def test_init_db_is_idempotent_across_reruns(tmp_db_path):
    """Running init_db a second time must not regress the version or duplicate
    the single schema_version row (the CHECK constraint would reject)."""
    import sqlite3

    from app.models._core import CURRENT_SCHEMA_VERSION

    models.init_db()  # second run
    with sqlite3.connect(tmp_db_path) as conn:
        rows = conn.execute("SELECT id, version FROM schema_version").fetchall()
    assert len(rows) == 1
    assert int(rows[0][1]) == CURRENT_SCHEMA_VERSION


def test_init_db_refuses_to_run_against_newer_schema(tmp_db_path):
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


def test_legacy_db_migrates_to_multiuser_schema(tmp_path, monkeypatch):
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


def test_v3_check_constraints_present_on_fresh_db(tmp_db_path):
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


def test_v3_check_rejects_oversized_passphrase(tmp_db_path):
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


def test_v3_check_rejects_oversized_label(tmp_db_path):
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


def test_v3_check_rejects_oversized_username(tmp_db_path):
    with (
        sqlite3.connect(str(tmp_db_path)) as conn,
        pytest.raises(sqlite3.IntegrityError),
    ):
        conn.execute(
            "INSERT INTO users (username, password_hash, totp_secret, "
            "created_at, updated_at) VALUES (?, 'h', 's', 'now', 'now')",
            ("u" * 257,),
        )


def test_v3_check_allows_null_optional_columns(tmp_db_path):
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


def _seed_v2_db(db_path):
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


def test_v2_legacy_db_clean_upgrades_to_v3(tmp_path, monkeypatch):
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


def test_v3_migration_preserves_users_autoincrement_no_reuse(tmp_path, monkeypatch):
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
# Schema v4 -- analytics_events table
# ---------------------------------------------------------------------------


def test_v4_analytics_events_table_present_on_fresh_db(tmp_db_path):
    """Fresh DBs land at v4 directly via TABLES_SCRIPT, which now includes
    the analytics_events table. The v4 migration is a pure-create no-op
    on fresh DBs (CREATE TABLE IF NOT EXISTS) and only fires meaningfully
    on legacy v3 DBs."""
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


def test_v4_analytics_events_columns_match_design(tmp_db_path):
    with sqlite3.connect(str(tmp_db_path)) as conn:
        cols = {
            r[1]: r[2]  # name -> type
            for r in conn.execute("PRAGMA table_info(analytics_events)").fetchall()
        }
    assert cols == {
        "id": "INTEGER",
        "event_type": "TEXT",
        "occurred_at": "TIMESTAMP",
        "user_id": "INTEGER",
        "payload": "TEXT",
    }


def test_v3_legacy_db_upgrades_to_v4(tmp_path, monkeypatch):
    """Seed a v3 DB (CHECK clauses present, no analytics_events table,
    schema_version stamped at 3), boot the current code, and confirm
    the v4 migration creates the analytics_events table without
    disturbing the v3 CHECK constraints."""
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
        assert ver == 4
        assert "analytics_events" in names
        # v3 CHECK clauses survive: the v4 migration is pure-create and
        # does not touch the secrets/users tables.
        assert "CHECK" in secrets_sql
    finally:
        config.get_settings.cache_clear()


def test_v2_legacy_db_with_violating_rows_aborts_v3_migration(tmp_path, monkeypatch):
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
