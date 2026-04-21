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


def test_lockout_counter_has_no_rolling_window():
    """The lockout threshold is a cumulative-since-last-success counter,
    NOT a rolling window. There used to be a `LOCKOUT_WINDOW_SECONDS`
    constant exported from `app.auth` that implied "failures within this
    window count"; the code never enforced that. Removed to stop the
    constant from suggesting behaviour that isn't there. This test pins
    the removal so it can't be re-introduced by accident."""
    from app import auth
    from app.auth import _core

    assert not hasattr(auth, "LOCKOUT_WINDOW_SECONDS")
    assert not hasattr(_core, "LOCKOUT_WINDOW_SECONDS")
    # Sanity: the constants that SHOULD be there are still there.
    assert auth.MAX_FAILURES == 10
    assert auth.LOCKOUT_DURATION_SECONDS == 3600


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
    # And the opt-in with-TOTP wrapper round-trips back to plaintext:
    assert (
        models.get_user_with_totp_by_id(provisioned_user["id"])["totp_secret"]
        == plaintext
    )


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
    assert (
        models.get_user_with_totp_by_id(provisioned_user["id"])["totp_secret"]
        == new_secret
    )


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


# ---------------------------------------------------------------------------
# Timing equalization between unknown-user and known-user failure paths
# ---------------------------------------------------------------------------


def test_unknown_user_runs_worst_case_bcrypt_count(provisioned_user, monkeypatch):
    """The known-user failure path can cost up to (1 + RECOVERY_CODE_COUNT)
    bcrypts: 1 for password verify + up to RECOVERY_CODE_COUNT for the
    recovery-code iteration when the submitted code isn't a valid 6-digit
    TOTP. The unknown-user path must do the same number of checkpws so a
    timing attacker can't tell "user exists" from "user doesn't" by
    response time.

    Rather than wall-clock timing (flaky), count bcrypt.checkpw calls
    directly."""
    import bcrypt as bcrypt_lib
    from app.auth import _core, login as login_mod

    count = [0]
    real_checkpw = bcrypt_lib.checkpw

    def counting_checkpw(*args, **kwargs):
        count[0] += 1
        return real_checkpw(*args, **kwargs)

    # Patch both the library and the import in login.py's namespace.
    monkeypatch.setattr(bcrypt_lib, "checkpw", counting_checkpw)
    monkeypatch.setattr(login_mod.bcrypt, "checkpw", counting_checkpw)

    # Unknown user, non-6-digit code (would trigger recovery-code path on a
    # real user). Must raise AuthError and burn the full worst-case count.
    count[0] = 0
    with pytest.raises(auth.AuthError):
        auth.authenticate("ghost-does-not-exist", "pw", "XXXXX-YYYYY")
    unknown_user_checkpws = count[0]
    assert unknown_user_checkpws == 1 + _core.RECOVERY_CODE_COUNT, (
        f"expected {1 + _core.RECOVERY_CODE_COUNT} bcrypt.checkpw calls on "
        f"unknown-user path, got {unknown_user_checkpws}"
    )

    # Known user, same shape of bad input (wrong password + non-6-digit code):
    # should do the same count -- 1 password check + 10 recovery-code checks.
    count[0] = 0
    with pytest.raises(auth.AuthError):
        auth.authenticate(provisioned_user["username"], "wrong", "XXXXX-YYYYY")
    known_user_checkpws = count[0]
    assert known_user_checkpws == unknown_user_checkpws, (
        f"known-user path did {known_user_checkpws} checkpws, "
        f"unknown-user path did {unknown_user_checkpws} -- must match."
    )


def test_known_user_wrong_password_correct_totp_runs_worst_case_bcrypt_count(
    provisioned_user, monkeypatch
):
    """The unknown-user failure path always burns (1 + RECOVERY_CODE_COUNT)
    bcrypts so response time doesn't reveal whether the username exists.
    The known-user + wrong-password branch matches that cost when the
    submitted code is non-TOTP (consume_backup_code iterates all stored
    hashes). But the known-user + wrong-password + VALID TOTP shape
    shortcuts to 1 bcrypt because verify_totp succeeds and
    consume_backup_code is skipped. Without padding that branch, an
    attacker with a captured TOTP could time the 401 to confirm a
    username exists.

    Mirrors test_unknown_user_runs_worst_case_bcrypt_count at the other
    axis of the symmetry."""
    import bcrypt as bcrypt_lib
    from app.auth import _core, login as login_mod

    count = [0]
    real_checkpw = bcrypt_lib.checkpw

    def counting_checkpw(*args, **kwargs):
        count[0] += 1
        return real_checkpw(*args, **kwargs)

    monkeypatch.setattr(bcrypt_lib, "checkpw", counting_checkpw)
    monkeypatch.setattr(login_mod.bcrypt, "checkpw", counting_checkpw)

    count[0] = 0
    valid_totp = provisioned_user["totp"].now()
    with pytest.raises(auth.AuthError):
        auth.authenticate(
            provisioned_user["username"], "wrong-password", valid_totp
        )

    expected = 1 + _core.RECOVERY_CODE_COUNT
    assert count[0] == expected, (
        f"known-user + wrong-password + valid-TOTP branch did {count[0]} "
        f"bcrypt.checkpw calls, expected {expected} (1 for the password "
        f"check + {_core.RECOVERY_CODE_COUNT} dummy pads so the timing "
        f"matches the unknown-user failure path)"
    )


def test_totp_last_step_bumped_even_on_wrong_password(provisioned_user):
    """A captured valid TOTP must become single-use even if the paired
    password is wrong. Otherwise an attacker with a phishing-stolen TOTP
    could re-submit it against multiple password guesses until lockout.
    The fix: persist totp_last_step the moment verify_totp returns a
    step, regardless of whether the overall login succeeds.

    Note: recovery codes are deliberately NOT consumed on failure (v3
    F3-06 thread 2). The asymmetry is justified because TOTP rotates
    every 30s while recovery codes don't -- bumping last_step on
    failure costs the victim at most a 30s wait, whereas burning a
    recovery code on failure creates a DoS surface on the rescue pool."""
    current_totp = provisioned_user["totp"].now()

    # Before: totp_last_step is zero (fresh user).
    assert models.get_user_by_id(provisioned_user["id"])["totp_last_step"] == 0

    # Right password + right TOTP would succeed. But send the TOTP with a
    # WRONG password; login must fail -- AND last_step must advance so
    # the same TOTP can't be replayed.
    with pytest.raises(auth.AuthError):
        auth.authenticate(provisioned_user["username"], "wrong-password", current_totp)

    row = models.get_user_by_id(provisioned_user["id"])
    assert row["totp_last_step"] > 0, (
        "totp_last_step must advance even when the paired password is wrong"
    )
    bumped_step = row["totp_last_step"]

    # Second attempt with the SAME captured TOTP + a different password
    # guess: the attacker's replay path. verify_totp should refuse the
    # replayed step because last_step is now >= it.
    with pytest.raises(auth.AuthError):
        auth.authenticate(
            provisioned_user["username"], "another-wrong-password", current_totp
        )

    # last_step hasn't been yanked backwards by the second attempt. It may
    # stay the same (verify_totp returned None for the replayed step, so
    # there was nothing new to persist), or it may have advanced to a
    # fresh live step if we happened to cross a 30s boundary. Either is
    # fine; the invariant is "must not regress."
    row2 = models.get_user_by_id(provisioned_user["id"])
    assert row2["totp_last_step"] >= bumped_step


def test_recovery_code_consumption_is_not_persisted_on_wrong_password(
    provisioned_user,
):
    """Contrast with test_totp_last_step_bumped_even_on_wrong_password.
    Recovery codes must remain valid after a wrong-password + correct-
    recovery-code failed login, so an attacker who knows a username
    can't DoS the victim's rescue pool via triggered failed logins.
    Industry convention (Google/GitHub/Cloudflare all consume on
    success only). v3 F3-06 thread 2 made this an explicit decision."""
    codes, blob = auth.generate_recovery_codes()
    models.update_user(provisioned_user["id"], recovery_code_hashes=blob)

    # Wrong password + correct recovery code -> auth fails.
    with pytest.raises(auth.AuthError):
        auth.authenticate(provisioned_user["username"], "wrong-password", codes[0])

    # The code is still usable -- a subsequent login with the RIGHT
    # password + same code must succeed.
    user = auth.authenticate(
        provisioned_user["username"], provisioned_user["password"], codes[0]
    )
    assert user["id"] == provisioned_user["id"]


def test_authenticate_success_return_does_not_contain_totp_secret(provisioned_user):
    """The models-layer split keeps the plaintext TOTP seed out of every
    read path whose name does NOT contain `with_totp` -- reading
    `user["totp_secret"]` off the default getter raises KeyError. The one
    remaining symbol that handed a with-TOTP dict back to callers was
    `authenticate()`'s success return. No caller reads the field today,
    but a future log line / error handler / telemetry hook that dumps
    the user dict would have leaked the seed. Pairs with
    test_default_user_getters_do_not_return_totp_secret at the models
    layer to pin: plaintext TOTP only flows through a symbol whose
    name contains `with_totp`.
    """
    current_totp = provisioned_user["totp"].now()
    user = auth.authenticate(
        provisioned_user["username"], provisioned_user["password"], current_totp
    )
    assert user["id"] == provisioned_user["id"]
    assert "totp_secret" not in user


def test_recovery_code_lookup_is_constant_time_across_consumption_state(
    provisioned_user, monkeypatch
):
    """consume_backup_code must run bcrypt.checkpw once per stored entry
    regardless of how many codes have been used. Without this invariant a
    timing attacker could distinguish between users by consumption state
    (a user with 10 fresh codes runs 10 checks; a user with only 1 code
    left used to run only 1). End-to-end via authenticate() to catch
    both the helper's loop AND the caller's wrapping behaviour."""
    import bcrypt as bcrypt_lib
    from app.auth import _core, login as login_mod

    # Freshly-minted recovery codes for Alice so we have known plaintexts.
    codes, blob = auth.generate_recovery_codes()
    models.update_user(provisioned_user["id"], recovery_code_hashes=blob)

    count = [0]
    real_checkpw = bcrypt_lib.checkpw

    def counting_checkpw(*args, **kwargs):
        count[0] += 1
        return real_checkpw(*args, **kwargs)

    monkeypatch.setattr(bcrypt_lib, "checkpw", counting_checkpw)
    monkeypatch.setattr(login_mod.bcrypt, "checkpw", counting_checkpw)

    # --- Baseline: failed login with 0 codes consumed --------------------
    count[0] = 0
    with pytest.raises(auth.AuthError):
        auth.authenticate(provisioned_user["username"], "wrong", "XXXXX-YYYYY")
    baseline_checkpws = count[0]
    assert baseline_checkpws == 1 + _core.RECOVERY_CODE_COUNT

    # --- Consume 4 codes via real successful logins ----------------------
    for used_code in codes[:4]:
        auth.authenticate(
            provisioned_user["username"], provisioned_user["password"], used_code
        )

    # --- Failed login again, now with 4 codes used ---------------------
    count[0] = 0
    with pytest.raises(auth.AuthError):
        auth.authenticate(provisioned_user["username"], "wrong", "XXXXX-YYYYY")
    after_4_checkpws = count[0]

    assert after_4_checkpws == baseline_checkpws, (
        f"consumption-state leak: baseline did {baseline_checkpws} checkpws, "
        f"after 4 consumed codes did {after_4_checkpws} -- must match."
    )

    # --- Consume all remaining codes, then probe with none unused -------
    for used_code in codes[4:]:
        auth.authenticate(
            provisioned_user["username"], provisioned_user["password"], used_code
        )

    count[0] = 0
    with pytest.raises(auth.AuthError):
        auth.authenticate(provisioned_user["username"], "wrong", "XXXXX-YYYYY")
    all_used_checkpws = count[0]
    assert all_used_checkpws == baseline_checkpws, (
        f"consumption-state leak at k=10: baseline {baseline_checkpws} vs "
        f"all-used {all_used_checkpws} -- must match."
    )


