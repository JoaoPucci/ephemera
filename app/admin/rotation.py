"""Credential-rotation commands: reset-password, rotate-totp,
regen-recovery-codes.

Each rotation bumps the target user's session_generation, which
invalidates every outstanding session cookie immediately -- the most
important property of these commands beyond the credential swap.
"""

from .. import auth, models
from . import _core


def cmd_reset_password(username: str | None) -> None:
    user = _core._resolve_user(username)
    _core._reauth(user)
    new_pw = _core._prompt_new_password()
    models.update_user(user["id"], password_hash=auth.hash_password(new_pw))
    models.bump_session_generation(user["id"])
    _core.audit("password.reset", user_id=user["id"], username=user["username"])
    print(f"password updated for '{user['username']}'.")
    print(
        "  live sessions for this user have been invalidated; they must log in again."
    )


def cmd_rotate_totp(username: str | None) -> None:
    user = _core._resolve_user(username)
    _core._reauth(user)
    secret = auth.generate_totp_secret()
    models.update_user(user["id"], totp_secret=secret, totp_last_step=0)
    models.bump_session_generation(user["id"])
    _core.audit("totp.rotated", user_id=user["id"], username=user["username"])
    _core._print_totp_setup(secret, user["username"])
    print(
        "new TOTP active. The old authenticator entry will stop working after you re-scan."
    )
    print(
        "  live sessions for this user have been invalidated; they must log in again."
    )


def cmd_regen_recovery_codes(username: str | None) -> None:
    user = _core._resolve_user(username)
    _core._reauth(user)
    codes, codes_json = auth.generate_recovery_codes()
    models.update_user(user["id"], recovery_code_hashes=codes_json)
    models.bump_session_generation(user["id"])
    _core.audit("recovery.regenerated", user_id=user["id"], username=user["username"])
    _core._print_recovery_codes(codes)
    print(
        "  live sessions for this user have been invalidated; they must log in again."
    )
