"""Tests for app.models: CRUD, expiry queries, tracking behavior, user scoping."""
from datetime import datetime, timedelta, timezone

import pytest

from app import models


def _utcnow():
    return datetime.now(timezone.utc)


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
    row = models.get_by_id(r["id"])
    assert row is not None
    assert row["status"] == "viewed"
    assert row["ciphertext"] is None
    assert row["server_key"] is None
    assert row["passphrase"] is None
    assert row["viewed_at"] is not None


def test_mark_viewed_on_untracked_deletes_row(provisioned_user):
    r = _mk(provisioned_user["id"])
    models.mark_viewed(r["id"])
    assert models.get_by_id(r["id"]) is None


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


def test_untrack_scopes_by_user(provisioned_user, make_user):
    bob = make_user("bob")
    r = _mk(provisioned_user["id"], track=True)
    # Bob cannot untrack Alice's secret.
    assert models.untrack(r["id"], bob["id"]) is False
    row = models.get_by_id(r["id"])
    assert row is not None and row["track"] == 1
    # Alice can.
    assert models.untrack(r["id"], provisioned_user["id"]) is True


def test_cascade_on_delete_user_drops_their_secrets_and_tokens(provisioned_user):
    r = _mk(provisioned_user["id"], track=True)
    from app import auth
    _, digest = auth.mint_api_token()
    models.create_token(user_id=provisioned_user["id"], name="t1", token_hash=digest)

    models.delete_user(provisioned_user["id"])
    assert models.get_by_id(r["id"]) is None
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
    with pytest.raises(Exception):
        models.create_user(
            username="alice",
            password_hash=auth.hash_password("pw12345678"),
            totp_secret=auth.generate_totp_secret(),
            recovery_code_hashes="[]",
        )


# ---------------------------------------------------------------------------
# Migration: legacy single-user DB upgrades cleanly
# ---------------------------------------------------------------------------


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
    sec = models.get_by_id("legacy-sid")
    assert sec is not None and sec["user_id"] == 1
    toks = models.list_tokens(1)
    assert len(toks) == 1 and toks[0]["name"] == "legacy-tok-name"

    config.get_settings.cache_clear()
