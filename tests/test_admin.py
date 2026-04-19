"""Tests for the admin CLI helpers that have non-trivial logic.

Stdin prompts are stubbed via monkeypatch; sys.exit raises SystemExit
which we catch with pytest.raises.
"""
import pytest

from app import admin, models


def _patch_input(monkeypatch, value):
    monkeypatch.setattr("builtins.input", lambda *a, **kw: value)


def test_force_removal_rejects_same_user(provisioned_user, monkeypatch, capsys):
    """Force mode demands a DIFFERENT user than the target."""
    _patch_input(monkeypatch, provisioned_user["username"])
    with pytest.raises(SystemExit) as exc:
        admin._reauth_for_force_removal(provisioned_user)
    assert exc.value.code == 1
    assert "different user" in capsys.readouterr().err.lower()


def test_force_removal_rejects_empty_username(provisioned_user, monkeypatch, capsys):
    """Empty input aborts -- no accidental skip of the authenticator prompt."""
    _patch_input(monkeypatch, "")
    with pytest.raises(SystemExit) as exc:
        admin._reauth_for_force_removal(provisioned_user)
    assert exc.value.code == 1
    assert "empty" in capsys.readouterr().err.lower()


def test_force_removal_rejects_unknown_authenticator(provisioned_user, monkeypatch, capsys):
    """Typing a username that doesn't exist fails fast."""
    _patch_input(monkeypatch, "ghost-nobody-here")
    with pytest.raises(SystemExit) as exc:
        admin._reauth_for_force_removal(provisioned_user)
    assert exc.value.code == 1
    assert "no user named" in capsys.readouterr().err.lower()


def test_force_removal_reauths_as_named_user(provisioned_user, make_user, monkeypatch):
    """Happy path: named user exists and is NOT the target -> _reauth runs against them."""
    bob = make_user("bob")
    _patch_input(monkeypatch, "bob")
    reauth_calls = []
    monkeypatch.setattr(admin, "_reauth", lambda user: reauth_calls.append(user["username"]))
    admin._reauth_for_force_removal(provisioned_user)
    assert reauth_calls == ["bob"]


def test_remove_user_refuses_to_empty_the_db(provisioned_user, capsys):
    """The only-remaining-user guard fires before either auth path."""
    with pytest.raises(SystemExit) as exc:
        admin.cmd_remove_user(provisioned_user["username"], force=True)
    assert exc.value.code == 1
    assert "only remaining user" in capsys.readouterr().err.lower()


def test_prompt_new_password_rejects_pwned_password(monkeypatch, capsys):
    """A password that the HIBP API reports as breached must be refused
    and re-prompted; the final accepted value is the one that comes back
    clean."""
    from app import admin
    from app.auth import hibp

    passwords = iter(["breachedpwned1!", "breachedpwned1!", "freshphrase-unique", "freshphrase-unique"])
    monkeypatch.setattr("getpass.getpass", lambda *a, **kw: next(passwords))
    counts = iter([99999, 0])
    monkeypatch.setattr(hibp, "pwned_count", lambda p, **kw: next(counts))

    result = admin._prompt_new_password()
    assert result == "freshphrase-unique"
    out = capsys.readouterr().out
    assert "99,999 known breaches" in out


def test_prompt_new_password_warns_and_accepts_when_hibp_unreachable(
    monkeypatch, capsys
):
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
    provisioned_user, make_user, monkeypatch
):
    """End-to-end in force mode: target is dropped, cascade fires, other user survives."""
    bob = make_user("bob")
    _patch_input(monkeypatch, "bob")
    monkeypatch.setattr(admin, "_reauth", lambda user: None)  # skip password/TOTP prompt

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
