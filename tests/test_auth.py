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
