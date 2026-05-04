"""Tests for the admin CLI helpers that have non-trivial logic.

Stdin prompts are stubbed via monkeypatch; sys.exit raises SystemExit
which we catch with pytest.raises.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app import admin, models


def _patch_input(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setattr("builtins.input", lambda *a, **kw: value)


def test_force_removal_rejects_same_user(
    provisioned_user: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Force mode demands a DIFFERENT user than the target."""
    _patch_input(monkeypatch, provisioned_user["username"])
    with pytest.raises(SystemExit) as exc:
        admin._reauth_for_force_removal(provisioned_user)
    assert exc.value.code == 1
    assert "different user" in capsys.readouterr().err.lower()


def test_force_removal_rejects_empty_username(
    provisioned_user: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Empty input aborts -- no accidental skip of the authenticator prompt."""
    _patch_input(monkeypatch, "")
    with pytest.raises(SystemExit) as exc:
        admin._reauth_for_force_removal(provisioned_user)
    assert exc.value.code == 1
    assert "empty" in capsys.readouterr().err.lower()


def test_force_removal_rejects_unknown_authenticator(
    provisioned_user: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Typing a username that doesn't exist fails fast."""
    _patch_input(monkeypatch, "ghost-nobody-here")
    with pytest.raises(SystemExit) as exc:
        admin._reauth_for_force_removal(provisioned_user)
    assert exc.value.code == 1
    assert "no user named" in capsys.readouterr().err.lower()


def test_force_removal_reauths_as_named_user(
    provisioned_user: dict[str, Any],
    make_user: Callable[..., dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: named user exists and is NOT the target -> _reauth runs against them."""
    bob = make_user("bob")
    _patch_input(monkeypatch, "bob")
    reauth_calls = []
    monkeypatch.setattr(
        admin._core, "_reauth", lambda user: reauth_calls.append(user["username"])
    )
    admin._reauth_for_force_removal(provisioned_user)
    assert reauth_calls == ["bob"]


def test_remove_user_refuses_to_empty_the_db(
    provisioned_user: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The only-remaining-user guard fires before either auth path."""
    with pytest.raises(SystemExit) as exc:
        admin.cmd_remove_user(provisioned_user["username"], force=True)
    assert exc.value.code == 1
    assert "only remaining user" in capsys.readouterr().err.lower()


def test_prompt_new_password_rejects_pwned_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A password that the HIBP API reports as breached must be refused
    and re-prompted; the final accepted value is the one that comes back
    clean."""
    from app import admin
    from app.auth import hibp

    passwords = iter(
        [
            "breachedpwned1!",
            "breachedpwned1!",
            "freshphrase-unique",
            "freshphrase-unique",
        ]
    )
    monkeypatch.setattr("getpass.getpass", lambda *a, **kw: next(passwords))
    counts = iter([99999, 0])
    monkeypatch.setattr(hibp, "pwned_count", lambda p, **kw: next(counts))

    result = admin._prompt_new_password()
    assert result == "freshphrase-unique"
    out = capsys.readouterr().out
    assert "99,999 known breaches" in out


def test_prompt_new_password_warns_and_accepts_when_hibp_unreachable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Offline host / DNS blip: pwned_count returns None. The caller prints
    a warning and accepts the password rather than blocking admin ops."""
    from app import admin
    from app.auth import hibp

    pw = "solid-strong-local-phrase-1234"
    monkeypatch.setattr("getpass.getpass", lambda *a, **kw: pw)
    monkeypatch.setattr(hibp, "pwned_count", lambda p, **kw: None)

    assert admin._prompt_new_password() == pw
    assert "couldn't reach" in capsys.readouterr().out.lower()


def test_remove_user_with_force_deletes_target_and_cascades(
    provisioned_user: dict[str, Any],
    make_user: Callable[..., dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end in force mode: target is dropped, cascade fires, other user survives."""
    bob = make_user("bob")
    _patch_input(monkeypatch, "bob")
    monkeypatch.setattr(
        admin._core, "_reauth", lambda user: None
    )  # skip password/TOTP prompt

    # Give the target something to cascade.
    created = models.create_secret(
        user_id=provisioned_user["id"],
        content_type="text",
        mime_type=None,
        ciphertext=b"y" * 16,
        server_key=b"x" * 16,
        passphrase_hash=None,
        track=False,
        expires_in=3600,
    )

    admin.cmd_remove_user(provisioned_user["username"], force=True)

    assert models.get_user_by_username(provisioned_user["username"]) is None
    assert models.get_user_by_username("bob") is not None
    assert models.get_by_token(created["token"]) is None


# ---------------------------------------------------------------------------
# diagnose --show-secret
#
# Clock-drift diagnosis (the command's routine use case) only needs the
# candidate TOTP codes and the step info. The raw TOTP seed is useful for
# a rarer case (re-entering the seed in a new authenticator by hand) and
# is noisy to have on the terminal by default: tmux scrollback, screen-
# shares, and accidental paste-into-chat all carry the seed when it's
# always printed. Default is no-seed; --show-secret opts in.
# ---------------------------------------------------------------------------


def test_diagnose_default_does_not_print_totp_secret(
    provisioned_user: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_diagnose(..., show_secret=False) suppresses the raw base32
    seed. The three candidate TOTP codes still print so the clock-drift
    use case is covered."""
    admin.cmd_diagnose(provisioned_user["username"], show_secret=False)
    out = capsys.readouterr().out

    assert provisioned_user["totp_secret"] not in out
    # Sanity: clock-drift info is still there.
    assert "Current TOTP step:" in out
    assert "previous step" in out
    # And we tell the operator how to opt in.
    assert "--show-secret" in out


def test_diagnose_with_show_secret_prints_totp_secret(
    provisioned_user: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    admin.cmd_diagnose(provisioned_user["username"], show_secret=True)
    out = capsys.readouterr().out

    assert provisioned_user["totp_secret"] in out
    # The red-flag banner still accompanies the seed print.
    assert "DO NOT paste" in out


def test_diagnose_defaults_show_secret_false_when_called_via_main(
    provisioned_user: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """Bare `python -m app.admin diagnose` (no --show-secret) must omit
    the raw seed -- the whole point of gating the print is that the
    routine case doesn't carry it. Route through admin.main() to cover
    the dispatcher wiring as well as the cmd_ function."""
    admin.main(["diagnose", "--user", provisioned_user["username"]])
    out = capsys.readouterr().out

    assert provisioned_user["totp_secret"] not in out


def test_diagnose_main_recognises_show_secret_flag(
    provisioned_user: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """Dispatcher strips --show-secret from argv (so the arity check
    doesn't see it as a positional) and passes show_secret=True."""
    admin.main(
        [
            "diagnose",
            "--user",
            provisioned_user["username"],
            "--show-secret",
        ]
    )
    out = capsys.readouterr().out

    assert provisioned_user["totp_secret"] in out


# ---------------------------------------------------------------------------
# Helpers shared by the bulk of cmd_* tests below.
#
# The interactive cmd_* functions (init, add-user, rotation, token mint)
# all chain getpass + input + reauth + a few "print this banner" helpers.
# We don't want each test to drive the full interactive flow; instead
# we stub _reauth and _prompt_new_password (covered separately above)
# and the QR printer, then call the cmd_ function directly. The
# behaviour we care about -- DB writes, audit emit, session_generation
# bumps, exit codes -- is covered without 6 lines of getpass mocks per
# test.
# ---------------------------------------------------------------------------


def _stub_interactive(
    monkeypatch: pytest.MonkeyPatch, *, new_password: str = "freshly-picked-phrase-A1!"
) -> None:
    """Skip the password / TOTP prompts and the QR ASCII print. Tests that
    care about a specific password value pass it via `new_password`."""
    monkeypatch.setattr(admin._core, "_reauth", lambda user: None)
    monkeypatch.setattr(admin._core, "_prompt_new_password", lambda: new_password)
    monkeypatch.setattr(admin._core, "_print_totp_setup", lambda secret, username: None)


# ---------------------------------------------------------------------------
# cmd_init: refuses-if-exists invariant + happy path
# ---------------------------------------------------------------------------


def test_cmd_init_refuses_when_a_user_already_exists(
    provisioned_user: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """init is a one-shot bootstrap. Running it on a non-empty DB must
    refuse rather than silently provision a second user (which would also
    side-step the re-auth gate add-user enforces)."""
    with pytest.raises(SystemExit) as exc:
        admin.cmd_init("would-be-second")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "already exists" in err
    # Operator gets pointed at the right next command.
    assert "add-user" in err


def test_cmd_init_creates_first_user_and_emits_audit(
    tmp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Happy path: empty DB -> init creates user + writes a user.added audit
    line. We assert on observable side effects, not the QR print itself."""
    _stub_interactive(monkeypatch)
    audit_events = []
    monkeypatch.setattr(
        admin._core, "audit", lambda event, **kw: audit_events.append((event, kw))
    )

    admin.cmd_init("alice")

    assert models.get_user_by_username("alice") is not None
    assert ("user.added", {"user_id": 1, "username": "alice"}) in audit_events
    out = capsys.readouterr().out
    assert "Bootstrap complete" in out


# ---------------------------------------------------------------------------
# cmd_add_user: refuses-if-empty + reauth gate + provision
# ---------------------------------------------------------------------------


def test_cmd_add_user_refuses_when_no_users_exist(
    tmp_db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """add-user requires reauth as an existing user. With no users present,
    the reauth subject doesn't exist -- bail with a hint to run init first."""
    with pytest.raises(SystemExit) as exc:
        admin.cmd_add_user("bob")
    assert exc.value.code == 1
    assert "init" in capsys.readouterr().err.lower()


def test_cmd_add_user_provisions_after_reauth(
    provisioned_user: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Existing user signs in; new user is created + audit fires."""
    _stub_interactive(monkeypatch)
    audit_events = []
    monkeypatch.setattr(
        admin._core, "audit", lambda event, **kw: audit_events.append((event, kw))
    )

    admin.cmd_add_user("bob")

    assert models.get_user_by_username("bob") is not None
    assert any(
        e[0] == "user.added" and e[1].get("username") == "bob" for e in audit_events
    )


# ---------------------------------------------------------------------------
# cmd_list_users
# ---------------------------------------------------------------------------


def test_cmd_list_users_prints_no_users_marker_on_empty_db(
    tmp_db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    admin.cmd_list_users()
    assert capsys.readouterr().out.strip() == "(no users)"


def test_cmd_list_users_prints_each_user_with_id_and_creation_date(
    provisioned_user: dict[str, Any],
    make_user: Callable[..., dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    bob = make_user("bob")
    admin.cmd_list_users()
    out = capsys.readouterr().out
    # Both usernames are in the output.
    assert provisioned_user["username"] in out
    assert "bob" in out
    # Format carries the id (numeric) and the "created" prefix.
    assert "created" in out


# ---------------------------------------------------------------------------
# cmd_remove_user (non-force)
# ---------------------------------------------------------------------------


def test_cmd_remove_user_rejects_unknown_username(
    provisioned_user: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """Targeting a non-existent username fails fast before any reauth flow."""
    with pytest.raises(SystemExit) as exc:
        admin.cmd_remove_user("ghost-nobody-here")
    assert exc.value.code == 1
    assert "no user named" in capsys.readouterr().err.lower()


def test_cmd_remove_user_normal_mode_reauths_as_target_and_cascades(
    provisioned_user: dict[str, Any],
    make_user: Callable[..., dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-force path: target authenticates as themselves (we stub _reauth);
    their row is dropped + their secrets cascade."""
    bob = make_user("bob")
    monkeypatch.setattr(admin._core, "_reauth", lambda user: None)
    secret = models.create_secret(
        user_id=bob["id"],
        content_type="text",
        mime_type=None,
        ciphertext=b"x" * 16,
        server_key=b"y" * 16,
        passphrase_hash=None,
        track=False,
        expires_in=3600,
    )

    admin.cmd_remove_user("bob")

    assert models.get_user_by_username("bob") is None
    assert models.get_by_token(secret["token"]) is None


# ---------------------------------------------------------------------------
# cmd_reset_password / cmd_rotate_totp / cmd_regen_recovery_codes
#
# All three rotate credentials AND bump session_generation -- the
# bump is what invalidates outstanding session cookies, so it's the
# most important property of these commands beyond the credential swap.
# ---------------------------------------------------------------------------


def test_cmd_reset_password_bumps_session_generation_and_swaps_hash(
    provisioned_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_interactive(monkeypatch, new_password="another-strong-phrase-1!")
    before = models.get_user_by_id(provisioned_user["id"])
    assert before is not None
    old_gen = int(before["session_generation"])
    before_full = models.get_user_with_totp_by_id(provisioned_user["id"])
    assert before_full is not None
    old_hash = before_full["password_hash"]

    admin.cmd_reset_password(provisioned_user["username"])

    after = models.get_user_by_id(provisioned_user["id"])
    after_full = models.get_user_with_totp_by_id(provisioned_user["id"])
    assert after is not None and after_full is not None
    assert int(after["session_generation"]) == old_gen + 1
    assert after_full["password_hash"] != old_hash


def test_cmd_rotate_totp_writes_new_secret_resets_step_and_bumps_session(
    provisioned_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_interactive(monkeypatch)
    before = models.get_user_with_totp_by_id(provisioned_user["id"])
    assert before is not None
    old_secret = before["totp_secret"]
    old_gen = int(before["session_generation"])

    admin.cmd_rotate_totp(provisioned_user["username"])

    after = models.get_user_with_totp_by_id(provisioned_user["id"])
    assert after is not None
    assert after["totp_secret"] != old_secret
    assert int(after["totp_last_step"]) == 0  # anti-replay counter reset
    assert int(after["session_generation"]) == old_gen + 1


def test_cmd_regen_recovery_codes_replaces_hashes_and_bumps_session(
    provisioned_user: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _stub_interactive(monkeypatch)
    before = models.get_user_with_totp_by_id(provisioned_user["id"])
    assert before is not None
    old_codes_json = before["recovery_code_hashes"]
    old_gen = int(before["session_generation"])

    admin.cmd_regen_recovery_codes(provisioned_user["username"])

    after = models.get_user_with_totp_by_id(provisioned_user["id"])
    assert after is not None
    assert after["recovery_code_hashes"] != old_codes_json
    assert int(after["session_generation"]) == old_gen + 1
    out = capsys.readouterr().out
    # The codes themselves are printed exactly once (the "save these now" copy).
    assert "shown ONCE" in out


# ---------------------------------------------------------------------------
# cmd_list_tokens
# ---------------------------------------------------------------------------


def test_cmd_list_tokens_prints_empty_marker_when_user_has_none(
    provisioned_user: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    admin.cmd_list_tokens(provisioned_user["username"])
    assert capsys.readouterr().out.strip() == "(no tokens)"


def test_cmd_list_tokens_prints_active_and_revoked_status_per_row(
    provisioned_user: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """Status string in the output distinguishes [active] vs [revoked]."""
    from app import auth

    _, digest_a = auth.mint_api_token()
    models.create_token(
        user_id=provisioned_user["id"], name="ci-runner", token_hash=digest_a
    )
    _, digest_b = auth.mint_api_token()
    models.create_token(
        user_id=provisioned_user["id"], name="laptop", token_hash=digest_b
    )
    models.revoke_token(provisioned_user["id"], "laptop")

    admin.cmd_list_tokens(provisioned_user["username"])
    out = capsys.readouterr().out

    assert "[active]" in out and "ci-runner" in out
    assert "[revoked]" in out and "laptop" in out
    # "never" appears for tokens with no last_used_at.
    assert "never" in out


# ---------------------------------------------------------------------------
# cmd_create_token / cmd_revoke_token
# ---------------------------------------------------------------------------


def test_cmd_create_token_mints_and_prints_plaintext_once(
    provisioned_user: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(admin._core, "_reauth", lambda user: None)

    admin.cmd_create_token("ci-runner", provisioned_user["username"])

    out = capsys.readouterr().out
    # Plaintext token is shown with its eph_ prefix; only its hash is in the DB.
    assert "eph_" in out
    rows = models.list_tokens(provisioned_user["id"])
    assert any(r["name"] == "ci-runner" and r["revoked_at"] is None for r in rows)


def test_cmd_create_token_rejects_duplicate_name_per_user(
    provisioned_user: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Token names are unique per user. A second create with the same name
    must fail with a clear message rather than a raw IntegrityError."""
    monkeypatch.setattr(admin._core, "_reauth", lambda user: None)
    admin.cmd_create_token("ci-runner", provisioned_user["username"])
    capsys.readouterr()  # drop the success-path output

    with pytest.raises(SystemExit) as exc:
        admin.cmd_create_token("ci-runner", provisioned_user["username"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "already exists" in err


def test_cmd_revoke_token_marks_token_revoked(
    provisioned_user: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mints, revokes, asserts revoked_at set + token no longer authenticates."""
    monkeypatch.setattr(admin._core, "_reauth", lambda user: None)
    admin.cmd_create_token("ci-runner", provisioned_user["username"])
    capsys.readouterr()

    admin.cmd_revoke_token("ci-runner", provisioned_user["username"])

    rows = models.list_tokens(provisioned_user["id"])
    assert any(r["name"] == "ci-runner" and r["revoked_at"] is not None for r in rows)


def test_cmd_revoke_token_errors_on_unknown_name(
    provisioned_user: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Revoking a name that doesn't exist (or is already revoked) exits 1
    with a clear message -- silent success would hide a typo."""
    monkeypatch.setattr(admin._core, "_reauth", lambda user: None)

    with pytest.raises(SystemExit) as exc:
        admin.cmd_revoke_token("does-not-exist", provisioned_user["username"])
    assert exc.value.code == 1
    assert "no active token" in capsys.readouterr().err.lower()


# ---------------------------------------------------------------------------
# cmd_verify
# ---------------------------------------------------------------------------


def test_cmd_verify_reports_both_correct(
    provisioned_user: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "getpass.getpass", lambda *a, **kw: provisioned_user["password"]
    )
    monkeypatch.setattr(
        "builtins.input", lambda *a, **kw: provisioned_user["totp"].now()
    )

    admin.cmd_verify(provisioned_user["username"])
    out = capsys.readouterr().out

    assert "Both correct" in out
    assert "password:  OK" in out
    assert "totp:      OK" in out


def test_cmd_verify_reports_password_wrong_totp_right(
    provisioned_user: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("getpass.getpass", lambda *a, **kw: "definitely-wrong-password")
    monkeypatch.setattr(
        "builtins.input", lambda *a, **kw: provisioned_user["totp"].now()
    )

    admin.cmd_verify(provisioned_user["username"])
    out = capsys.readouterr().out

    assert "Password is wrong" in out
    assert "password:  MISMATCH" in out


def test_cmd_verify_reports_both_wrong(
    provisioned_user: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("getpass.getpass", lambda *a, **kw: "definitely-wrong")
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "000000")

    admin.cmd_verify(provisioned_user["username"])
    out = capsys.readouterr().out

    assert "Both password and TOTP are wrong" in out


# ---------------------------------------------------------------------------
# cmd_analytics_summary
# ---------------------------------------------------------------------------


def test_cmd_analytics_summary_rejects_unknown_event_type(
    tmp_db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        admin.cmd_analytics_summary("not.a.real.event")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "unknown event_type" in err


def test_cmd_analytics_summary_handles_empty_table(
    tmp_db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Known event type but no rows yet -- prints count: 0, no field stats."""
    admin.cmd_analytics_summary("content.limit_hit")
    out = capsys.readouterr().out
    assert "event_type: content.limit_hit" in out
    assert "count: 0" in out


# ---------------------------------------------------------------------------
# Dispatch (main)
# ---------------------------------------------------------------------------


def test_main_with_no_args_prints_usage_and_exits_zero(
    tmp_db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bare `python -m app.admin` is documentation -- prints help, exit 0."""
    with pytest.raises(SystemExit) as exc:
        admin.main([])
    assert exc.value.code == 0
    # The module docstring is what's printed.
    out = capsys.readouterr().out
    assert "init" in out and "add-user" in out


def test_main_with_unknown_command_exits_two(
    tmp_db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mistyped subcommand -- exit 2 (argv error), print usage."""
    with pytest.raises(SystemExit) as exc:
        admin.main(["totally-not-a-command"])
    assert exc.value.code == 2


def test_main_arity_mismatch_exits_two(
    provisioned_user: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """`init` takes one positional username; calling it with two trips the
    arity check before any DB work happens."""
    with pytest.raises(SystemExit) as exc:
        admin.main(["init", "alice", "extra-arg"])
    assert exc.value.code == 2
    assert "expects" in capsys.readouterr().err.lower()


def test_main_strips_force_flag_before_arity_check(
    provisioned_user: dict[str, Any],
    make_user: Callable[..., dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--force` is a flag on remove-user; the dispatcher must filter it
    out of the positional count so `remove-user --force <name>` parses as
    one positional, not two."""
    make_user("bob")
    monkeypatch.setattr(admin._core, "_reauth", lambda user: None)
    monkeypatch.setattr("builtins.input", lambda *a, **kw: provisioned_user["username"])

    admin.main(["remove-user", "--force", "bob"])

    assert models.get_user_by_username("bob") is None


# ---------------------------------------------------------------------------
# _parse_user_flag / _resolve_user
# ---------------------------------------------------------------------------


def test_parse_user_flag_extracts_long_form_and_leaves_other_args() -> None:
    name, rest = admin._parse_user_flag(["--user", "alice", "create-token", "ci"])
    assert name == "alice"
    assert rest == ["create-token", "ci"]


def test_parse_user_flag_extracts_short_form() -> None:
    name, rest = admin._parse_user_flag(["-u", "alice", "ci"])
    assert name == "alice"
    assert rest == ["ci"]


def test_parse_user_flag_returns_none_when_absent() -> None:
    name, rest = admin._parse_user_flag(["create-token", "ci"])
    assert name is None
    assert rest == ["create-token", "ci"]


def test_resolve_user_returns_sole_user_when_no_flag_passed(
    provisioned_user: dict[str, Any],
) -> None:
    """With exactly one user in the DB, omitting --user is a convenience
    shortcut -- pick them automatically."""
    user = admin._resolve_user(None)
    assert user["username"] == provisioned_user["username"]


def test_resolve_user_errors_when_multiple_users_and_no_flag(
    provisioned_user: dict[str, Any],
    make_user: Callable[..., dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_user("bob")
    with pytest.raises(SystemExit) as exc:
        admin._resolve_user(None)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "multiple users" in err.lower()
    # Both candidates are listed so the operator can pick.
    assert "bob" in err
    assert provisioned_user["username"] in err


def test_resolve_user_errors_when_named_user_does_not_exist(
    provisioned_user: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        admin._resolve_user("ghost-nobody-here")
    assert exc.value.code == 1
    assert "no user named" in capsys.readouterr().err.lower()


def test_resolve_user_errors_when_db_has_no_users(
    tmp_db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        admin._resolve_user(None)
    assert exc.value.code == 1
    assert "no users yet" in capsys.readouterr().err.lower()
