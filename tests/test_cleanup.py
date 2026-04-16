"""Tests for the cleanup module."""
from datetime import timedelta

import pytest

from app import cleanup, models


def test_run_cleanup_purges_expired_rows(tmp_db_path):
    stale = models.create_secret(
        content_type="text", mime_type=None, ciphertext=b"c",
        server_key=b"\x01" * 16, passphrase_hash=None, track=False, expires_in=-60,
    )
    fresh = models.create_secret(
        content_type="text", mime_type=None, ciphertext=b"c",
        server_key=b"\x01" * 16, passphrase_hash=None, track=False, expires_in=3600,
    )
    cleanup.run_once()
    assert models.get_by_token(stale["token"]) is None
    assert models.get_by_token(fresh["token"]) is not None


def test_run_cleanup_purges_tracked_metadata_past_retention(tmp_db_path):
    from datetime import datetime, timezone

    r = models.create_secret(
        content_type="text", mime_type=None, ciphertext=b"c",
        server_key=b"\x01" * 16, passphrase_hash=None, track=True, expires_in=3600,
    )
    models.mark_viewed(r["id"])
    old = (datetime.now(timezone.utc) - timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%SZ")
    models._force_viewed_at(r["id"], old)
    cleanup.run_once()
    assert models.get_status(r["id"]) is None
