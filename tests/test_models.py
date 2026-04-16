"""Tests for app.models: CRUD, expiry queries, tracking behavior."""
from datetime import datetime, timedelta, timezone

import pytest

from app import models


def _utcnow():
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_init_db_creates_secrets_table(tmp_db_path):
    import sqlite3

    with sqlite3.connect(tmp_db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='secrets'"
        ).fetchall()
    assert rows == [("secrets",)]


def test_create_secret_returns_id_token_and_expires_at(tmp_db_path):
    result = models.create_secret(
        content_type="text",
        mime_type=None,
        ciphertext=b"cipher",
        server_key=b"\x01" * 16,
        passphrase_hash=None,
        track=False,
        expires_in=3600,
    )
    assert "id" in result
    assert "token" in result
    assert "expires_at" in result
    assert len(result["token"]) >= 16


def test_token_is_unique(tmp_db_path):
    tokens = set()
    for _ in range(30):
        r = models.create_secret(
            content_type="text", mime_type=None, ciphertext=b"c",
            server_key=b"\x01" * 16, passphrase_hash=None, track=False, expires_in=3600,
        )
        tokens.add(r["token"])
    assert len(tokens) == 30


def test_get_by_token_returns_row(tmp_db_path):
    r = models.create_secret(
        content_type="text", mime_type=None, ciphertext=b"cipher",
        server_key=b"\x01" * 16, passphrase_hash=None, track=False, expires_in=3600,
    )
    row = models.get_by_token(r["token"])
    assert row is not None
    assert row["id"] == r["id"]
    assert row["ciphertext"] == b"cipher"
    assert row["content_type"] == "text"


def test_get_by_missing_token_returns_none(tmp_db_path):
    assert models.get_by_token("does-not-exist") is None


def test_delete_secret_removes_row(tmp_db_path):
    r = models.create_secret(
        content_type="text", mime_type=None, ciphertext=b"c",
        server_key=b"\x01" * 16, passphrase_hash=None, track=False, expires_in=3600,
    )
    models.delete_secret(r["id"])
    assert models.get_by_token(r["token"]) is None


def test_mark_viewed_on_tracked_nulls_payload_keeps_metadata(tmp_db_path):
    r = models.create_secret(
        content_type="text", mime_type=None, ciphertext=b"c",
        server_key=b"\x01" * 16, passphrase_hash="hash", track=True, expires_in=3600,
    )
    models.mark_viewed(r["id"])
    row = models.get_by_id(r["id"])
    assert row is not None
    assert row["status"] == "viewed"
    assert row["ciphertext"] is None
    assert row["server_key"] is None
    assert row["passphrase"] is None
    assert row["viewed_at"] is not None


def test_mark_viewed_on_untracked_deletes_row(tmp_db_path):
    r = models.create_secret(
        content_type="text", mime_type=None, ciphertext=b"c",
        server_key=b"\x01" * 16, passphrase_hash=None, track=False, expires_in=3600,
    )
    models.mark_viewed(r["id"])
    assert models.get_by_id(r["id"]) is None


def test_get_status_returns_pending_before_view(tmp_db_path):
    r = models.create_secret(
        content_type="text", mime_type=None, ciphertext=b"c",
        server_key=b"\x01" * 16, passphrase_hash=None, track=True, expires_in=3600,
    )
    status = models.get_status(r["id"])
    assert status is not None
    assert status["status"] == "pending"


def test_get_status_returns_viewed_after_reveal_on_tracked(tmp_db_path):
    r = models.create_secret(
        content_type="text", mime_type=None, ciphertext=b"c",
        server_key=b"\x01" * 16, passphrase_hash=None, track=True, expires_in=3600,
    )
    models.mark_viewed(r["id"])
    status = models.get_status(r["id"])
    assert status is not None
    assert status["status"] == "viewed"
    assert status["viewed_at"] is not None


def test_get_status_returns_none_for_untracked(tmp_db_path):
    r = models.create_secret(
        content_type="text", mime_type=None, ciphertext=b"c",
        server_key=b"\x01" * 16, passphrase_hash=None, track=False, expires_in=3600,
    )
    assert models.get_status(r["id"]) is None


def test_increment_attempts_increments_counter(tmp_db_path):
    r = models.create_secret(
        content_type="text", mime_type=None, ciphertext=b"c",
        server_key=b"\x01" * 16, passphrase_hash="hash", track=False, expires_in=3600,
    )
    assert models.increment_attempts(r["id"]) == 1
    assert models.increment_attempts(r["id"]) == 2
    assert models.increment_attempts(r["id"]) == 3


def test_purge_expired_removes_expired_rows(tmp_db_path):
    fresh = models.create_secret(
        content_type="text", mime_type=None, ciphertext=b"c",
        server_key=b"\x01" * 16, passphrase_hash=None, track=False, expires_in=3600,
    )
    stale = models.create_secret(
        content_type="text", mime_type=None, ciphertext=b"c",
        server_key=b"\x01" * 16, passphrase_hash=None, track=False, expires_in=-60,
    )
    purged = models.purge_expired()
    assert purged >= 1
    assert models.get_by_token(fresh["token"]) is not None
    assert models.get_by_token(stale["token"]) is None


def test_purge_tracked_metadata_after_retention_window(tmp_db_path):
    r = models.create_secret(
        content_type="text", mime_type=None, ciphertext=b"c",
        server_key=b"\x01" * 16, passphrase_hash=None, track=True, expires_in=3600,
    )
    models.mark_viewed(r["id"])
    models._force_viewed_at(r["id"], _iso(_utcnow() - timedelta(days=31)))
    purged = models.purge_tracked_metadata(retention_seconds=30 * 86400)
    assert purged == 1
    assert models.get_status(r["id"]) is None


def test_is_expired_returns_true_after_expires_at(tmp_db_path):
    r = models.create_secret(
        content_type="text", mime_type=None, ciphertext=b"c",
        server_key=b"\x01" * 16, passphrase_hash=None, track=False, expires_in=-1,
    )
    row = models.get_by_token(r["token"])
    assert models.is_expired(row) is True


def test_is_expired_returns_false_for_fresh_secret(tmp_db_path):
    r = models.create_secret(
        content_type="text", mime_type=None, ciphertext=b"c",
        server_key=b"\x01" * 16, passphrase_hash=None, track=False, expires_in=3600,
    )
    row = models.get_by_token(r["token"])
    assert models.is_expired(row) is False
