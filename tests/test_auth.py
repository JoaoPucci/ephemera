"""Tests for app.auth: password, TOTP skew+replay, backup codes, lockout, users, tokens."""
import json
import time

import pytest

from app import auth, models


def test_hash_and_verify_password_roundtrip():
    h = auth.hash_password("correct horse battery")
    assert auth.verify_password("correct horse battery", h) is True
    assert auth.verify_password("wrong", h) is False


def test_bcrypt_hash_has_standard_prefix():
    h = auth.hash_password("x")
    assert h.startswith("$2")


def test_verify_password_returns_false_for_malformed_hash():
    """bcrypt.checkpw raises ValueError on non-bcrypt strings (e.g. a legacy
    plaintext column, a truncated hash). Should return False, not crash."""
    assert auth.verify_password("anything", "not-a-bcrypt-hash") is False
    assert auth.verify_password("anything", "") is False


def test_totp_accepts_current_step(provisioned_user):
    secret = provisioned_user["totp_secret"]
    code = provisioned_user["totp"].now()
    step = auth.verify_totp(secret, code, last_step=0)
    assert step is not None and step > 0


def test_totp_rejects_wrong_code(provisioned_user):
    assert auth.verify_totp(provisioned_user["totp_secret"], "000000", last_step=0) is None


def test_totp_rejects_non_numeric(provisioned_user):
    assert auth.verify_totp(provisioned_user["totp_secret"], "abcdef", last_step=0) is None


def test_totp_accepts_previous_step_within_tolerance(provisioned_user):
    secret = provisioned_user["totp_secret"]
    prev_step_time = (int(time.time()) // auth.TOTP_INTERVAL - 1) * auth.TOTP_INTERVAL
    old_code = provisioned_user["totp"].at(prev_step_time)
    assert auth.verify_totp(secret, old_code, last_step=0) is not None


def test_totp_replay_blocked(provisioned_user):
    secret = provisioned_user["totp_secret"]
    code = provisioned_user["totp"].now()
    step = auth.verify_totp(secret, code, last_step=0)
    assert step is not None
    assert auth.verify_totp(secret, code, last_step=step) is None


def test_totp_rejects_step_far_in_past(provisioned_user):
    secret = provisioned_user["totp_secret"]
    ancient = provisioned_user["totp"].at(int(time.time()) - 300)
    assert auth.verify_totp(secret, ancient, last_step=0) is None


# ---------------------------------------------------------------------------
# Backup / recovery codes
# ---------------------------------------------------------------------------


def test_generate_recovery_codes_returns_10_codes_and_stores_hashes(tmp_db_path):
    codes, blob = auth.generate_recovery_codes()
    assert len(codes) == 10
    entries = json.loads(blob)
    assert len(entries) == 10
    assert all(e["used_at"] is None for e in entries)
    assert all(e["hash"].startswith("$2") for e in entries)


def test_consume_backup_code_marks_used(tmp_db_path):
    codes, blob = auth.generate_recovery_codes()
    updated = auth.consume_backup_code(codes[0], blob)
    assert updated is not None
    entries = json.loads(updated)
    used = [e for e in entries if e["used_at"] is not None]
    assert len(used) == 1


def test_consume_backup_code_is_single_use(tmp_db_path):
    codes, blob = auth.generate_recovery_codes()
    after_first = auth.consume_backup_code(codes[0], blob)
    assert after_first is not None
    assert auth.consume_backup_code(codes[0], after_first) is None


def test_consume_backup_code_rejects_malformed_json(tmp_db_path):
    """A JSON blob that doesn't parse returns None (no crash)."""
    assert auth.consume_backup_code("XXXXX-YYYYY", "not-json") is None


def test_consume_backup_code_skips_malformed_bcrypt_entries(tmp_db_path):
    """If one entry has a corrupted hash, we should skip it and try the rest
    rather than abort. Mirrors the same bcrypt-raises-ValueError defensive
    path verify_password uses."""
    codes, blob = auth.generate_recovery_codes()
    entries = json.loads(blob)
    # Corrupt the first entry's hash while keeping the second valid.
    entries[0]["hash"] = "not-a-bcrypt-hash"
    tampered = json.dumps(entries)
    # The second code should still be consumable despite the malformed first.
    updated = auth.consume_backup_code(codes[1], tampered)
    assert updated is not None


def test_consume_backup_code_rejects_unknown_code(tmp_db_path):
    _, blob = auth.generate_recovery_codes()
    assert auth.consume_backup_code("WRONG-CODE1", blob) is None


# ---------------------------------------------------------------------------
# End-to-end authenticate()
# ---------------------------------------------------------------------------


def test_authenticate_accepts_password_and_totp(provisioned_user):
    user = auth.authenticate(
        provisioned_user["username"], provisioned_user["password"], provisioned_user["totp"].now()
    )
    assert user["id"] == provisioned_user["id"]


def test_authenticate_rejects_unknown_username(provisioned_user):
    with pytest.raises(auth.AuthError):
        auth.authenticate("nobody", provisioned_user["password"], provisioned_user["totp"].now())


def test_authenticate_rejects_wrong_password(provisioned_user):
    with pytest.raises(auth.AuthError):
        auth.authenticate(provisioned_user["username"], "wrong", provisioned_user["totp"].now())


def test_authenticate_rejects_wrong_code(provisioned_user):
    with pytest.raises(auth.AuthError):
        auth.authenticate(provisioned_user["username"], provisioned_user["password"], "000000")


def test_authenticate_with_backup_code_works_once(provisioned_user):
    codes, blob = auth.generate_recovery_codes()
    models.update_user(provisioned_user["id"], recovery_code_hashes=blob)
    auth.authenticate(provisioned_user["username"], provisioned_user["password"], codes[0])
    with pytest.raises(auth.AuthError):
        auth.authenticate(provisioned_user["username"], provisioned_user["password"], codes[0])


def test_authenticate_resets_failed_attempts_on_success(provisioned_user):
    for _ in range(3):
        with pytest.raises(auth.AuthError):
            auth.authenticate(provisioned_user["username"], "wrong", "000000")
    assert models.get_user_by_id(provisioned_user["id"])["failed_attempts"] == 3
    auth.authenticate(
        provisioned_user["username"], provisioned_user["password"], provisioned_user["totp"].now()
    )
    assert models.get_user_by_id(provisioned_user["id"])["failed_attempts"] == 0


def test_lockout_after_max_failures(provisioned_user):
    for _ in range(auth.MAX_FAILURES):
        with pytest.raises(auth.AuthError):
            auth.authenticate(provisioned_user["username"], "wrong", "000000")
    with pytest.raises(auth.LockoutError):
        auth.authenticate(
            provisioned_user["username"], provisioned_user["password"], provisioned_user["totp"].now()
        )


def test_lockout_is_per_user(provisioned_user, make_user):
    """Locking Alice doesn't lock Bob."""
    bob = make_user("bob")
    for _ in range(auth.MAX_FAILURES):
        with pytest.raises(auth.AuthError):
            auth.authenticate(provisioned_user["username"], "wrong", "000000")
    # Alice is locked.
    with pytest.raises(auth.LockoutError):
        auth.authenticate(
            provisioned_user["username"], provisioned_user["password"], provisioned_user["totp"].now()
        )
    # Bob still fine.
    user = auth.authenticate(bob["username"], bob["password"], bob["totp"].now())
    assert user["id"] == bob["id"]


# ---------------------------------------------------------------------------
# API tokens
# ---------------------------------------------------------------------------


def test_api_token_mint_and_lookup(provisioned_user):
    plaintext, digest = auth.mint_api_token()
    assert plaintext.startswith("eph_")
    models.create_token(user_id=provisioned_user["id"], name="t1", token_hash=digest)
    row = auth.lookup_api_token(plaintext)
    assert row is not None and row["name"] == "t1" and row["user_id"] == provisioned_user["id"]


def test_api_token_lookup_rejects_unknown(provisioned_user):
    assert auth.lookup_api_token("eph_unknown") is None


def test_api_token_lookup_rejects_revoked(provisioned_user):
    plaintext, digest = auth.mint_api_token()
    models.create_token(user_id=provisioned_user["id"], name="t1", token_hash=digest)
    models.revoke_token(provisioned_user["id"], "t1")
    assert auth.lookup_api_token(plaintext) is None


def test_api_token_lookup_updates_last_used(provisioned_user):
    plaintext, digest = auth.mint_api_token()
    models.create_token(user_id=provisioned_user["id"], name="t1", token_hash=digest)
    before = models.list_tokens(provisioned_user["id"])[0]["last_used_at"]
    auth.lookup_api_token(plaintext)
    after = models.list_tokens(provisioned_user["id"])[0]["last_used_at"]
    assert before is None and after is not None


def test_token_name_unique_per_user_not_global(provisioned_user, make_user):
    """Alice and Bob can both have an API token named 'cli'."""
    bob = make_user("bob")
    _, d1 = auth.mint_api_token()
    _, d2 = auth.mint_api_token()
    models.create_token(user_id=provisioned_user["id"], name="cli", token_hash=d1)
    # This should NOT fail -- different user.
    models.create_token(user_id=bob["id"], name="cli", token_hash=d2)
    assert len(models.list_tokens(provisioned_user["id"])) == 1
    assert len(models.list_tokens(bob["id"])) == 1


# ---------------------------------------------------------------------------
# HIBP pwned-password check
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal context-manager-compatible stand-in for urlopen()'s return."""
    def __init__(self, body: str, status: int = 200):
        self._body = body.encode("ascii")
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _sha1_parts(password: str) -> tuple[str, str]:
    import hashlib
    h = hashlib.sha1(password.encode()).hexdigest().upper()
    return h[:5], h[5:]


def test_pwned_count_returns_count_on_corpus_hit(monkeypatch):
    """A known-breached password must round-trip through the range API
    into a non-zero count."""
    from app.auth import hibp

    _, suffix = _sha1_parts("password123")
    body = f"{suffix}:42\r\n{'F' * 35}:1\r\n"
    monkeypatch.setattr(
        hibp.urllib.request, "urlopen", lambda *a, **kw: _FakeResponse(body)
    )
    assert hibp.pwned_count("password123") == 42


def test_pwned_count_returns_zero_when_suffix_absent(monkeypatch):
    """Password not in the corpus -> 0 (fail-open with explicit False)."""
    from app.auth import hibp

    body = "0000000000000000000000000000000000A:5\r\n" + "1" * 35 + ":3\r\n"
    monkeypatch.setattr(
        hibp.urllib.request, "urlopen", lambda *a, **kw: _FakeResponse(body)
    )
    assert hibp.pwned_count("fresh-strong-unique-phrase-xyz") == 0


def test_pwned_count_returns_none_on_network_failure(monkeypatch):
    """An offline host (no DNS, no route) must not block password setup.
    None is the sentinel the caller uses to skip the check with a warning."""
    from app.auth import hibp

    def _boom(*a, **kw):
        raise hibp.urllib.error.URLError("network down")

    monkeypatch.setattr(hibp.urllib.request, "urlopen", _boom)
    assert hibp.pwned_count("anything") is None


def test_pwned_count_returns_none_on_non_200_status(monkeypatch):
    from app.auth import hibp

    monkeypatch.setattr(
        hibp.urllib.request,
        "urlopen",
        lambda *a, **kw: _FakeResponse("", status=503),
    )
    assert hibp.pwned_count("anything") is None


def test_provisioning_uri_respects_custom_issuer():
    """Different instances (dev / prod) need distinct issuer strings so
    their entries don't visually collide in a shared authenticator app."""
    secret = auth.generate_totp_secret()
    uri = auth.provisioning_uri(secret, account_name="admin", issuer="ephemera-dev")
    # The issuer appears twice: as the path prefix and as a query param.
    assert "ephemera-dev" in uri
    assert "issuer=ephemera-dev" in uri


def test_provisioning_uri_default_issuer_unchanged():
    """Keep backward compatibility: callers that don't pass issuer still
    get 'ephemera', so existing QRs remain reproducible."""
    secret = auth.generate_totp_secret()
    uri = auth.provisioning_uri(secret, account_name="admin")
    assert "issuer=ephemera" in uri


# ---------------------------------------------------------------------------
# TOTP at rest
# ---------------------------------------------------------------------------


def test_totp_secret_at_rest_is_not_plaintext(provisioned_user, tmp_db_path):
    """Invariant: the stored totp_secret is NEVER the base32 plaintext.
    Raw SQL reads must return the versioned ciphertext prefix; the model
    layer handles encrypt-on-write and decrypt-on-read transparently."""
    import sqlite3

    plaintext = provisioned_user["totp_secret"]
    with sqlite3.connect(tmp_db_path) as conn:
        stored, = conn.execute(
            "SELECT totp_secret FROM users WHERE id = ?", (provisioned_user["id"],)
        ).fetchone()
    assert stored != plaintext
    assert stored.startswith("v1:"), f"expected v1: prefix, got {stored!r}"
    # And the model wrapper round-trips back to plaintext:
    assert models.get_user_by_id(provisioned_user["id"])["totp_secret"] == plaintext


def test_rotate_totp_writes_ciphertext(provisioned_user, tmp_db_path):
    """After `update_user(totp_secret=...)` the DB cell still holds
    ciphertext -- no code path leaves a plaintext seed sitting on disk."""
    import sqlite3

    new_secret = auth.generate_totp_secret()
    models.update_user(provisioned_user["id"], totp_secret=new_secret)
    with sqlite3.connect(tmp_db_path) as conn:
        stored, = conn.execute(
            "SELECT totp_secret FROM users WHERE id = ?", (provisioned_user["id"],)
        ).fetchone()
    assert stored.startswith("v1:")
    assert stored != new_secret
    assert models.get_user_by_id(provisioned_user["id"])["totp_secret"] == new_secret


def test_secret_key_rotation_breaks_totp_but_recovery_code_still_works(
    provisioned_user, monkeypatch
):
    """Documented recovery path for at-rest TOTP encryption: if SECRET_KEY
    rotates, the stored TOTP ciphertext is undecryptable. The user must
    then log in with a recovery code (unaffected by the KEK change), after
    which `rotate-totp` writes a fresh seed under the new key. Regression-
    gate the recovery path so it can never silently break."""
    from app import config

    # Generate a recovery code set BEFORE rotation so bcrypt hashes are intact.
    codes, codes_json = auth.generate_recovery_codes()
    models.update_user(provisioned_user["id"], recovery_code_hashes=codes_json)

    monkeypatch.setenv("EPHEMERA_SECRET_KEY", "a-brand-new-key-9876543210abcdef")
    config.get_settings.cache_clear()
    try:
        # TOTP path should fail gracefully -- not crash the login handler.
        with pytest.raises(auth.AuthError):
            auth.authenticate(
                provisioned_user["username"],
                provisioned_user["password"],
                provisioned_user["totp"].now(),
            )
        # Recovery code rescue path must still work.
        user = auth.authenticate(
            provisioned_user["username"], provisioned_user["password"], codes[0]
        )
        assert user["username"] == provisioned_user["username"]
    finally:
        config.get_settings.cache_clear()


def test_legacy_plaintext_totp_secret_is_migrated_on_init_db(tmp_path, monkeypatch):
    """A DB rescued from before the at-rest rollout has a plaintext base32
    totp_secret. init_db() must encrypt it in place, idempotently."""
    import sqlite3
    from app import models

    db_path = tmp_path / "legacy.db"
    monkeypatch.setenv("EPHEMERA_DB_PATH", str(db_path))
    monkeypatch.setenv("EPHEMERA_SECRET_KEY", "legacy-migration-test-xxxxxxxxxxxxx")
    from app import config
    config.get_settings.cache_clear()

    plaintext = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"  # valid base32, 32 chars
    models.init_db()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO users (username, password_hash, totp_secret,
                                   recovery_code_hashes, created_at, updated_at)
               VALUES ('legacy', 'h', ?, '[]', 't', 't')""",
            (plaintext,),
        )

    # Second init_db picks the row up and rewrites it.
    models.init_db()
    with sqlite3.connect(db_path) as conn:
        stored, = conn.execute("SELECT totp_secret FROM users WHERE username = 'legacy'").fetchone()
    assert stored.startswith("v1:")
    assert stored != plaintext

    # Third init_db is a no-op -- the row stays exactly as rewritten.
    prior = stored
    models.init_db()
    with sqlite3.connect(db_path) as conn:
        again, = conn.execute("SELECT totp_secret FROM users WHERE username = 'legacy'").fetchone()
    assert again == prior

    config.get_settings.cache_clear()


def test_check_not_locked_passes_when_lockout_already_expired():
    """A lockout_until timestamp in the past (e.g., a stale lockout that
    wasn't cleared after its window elapsed) shouldn't block auth — the
    gate should silently pass through."""
    from datetime import datetime, timedelta, timezone
    from app.auth.lockout import check_not_locked

    past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    user = {"lockout_until": past}
    check_not_locked(user)  # must not raise


def test_check_not_locked_passes_when_no_lockout_set():
    """Happy path: no lockout_until at all -> pass through."""
    from app.auth.lockout import check_not_locked

    check_not_locked({"lockout_until": None})
    check_not_locked({})  # missing key entirely also fine
