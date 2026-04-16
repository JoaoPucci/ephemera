"""CLI for single-user provisioning and token management.

Usage:
    python -m app.admin <command> [args]

Commands:
    init                     Create the user (password + TOTP + recovery codes).
    reset-password           Change password (requires TOTP).
    rotate-totp              Generate a new TOTP secret (requires password).
    regen-recovery-codes     Print 10 fresh recovery codes (requires password+TOTP).
    list-tokens              Show all API tokens.
    create-token <name>      Mint a new API token (requires password+TOTP).
    revoke-token <name>      Revoke a token by name.

All "sensitive" commands require re-authentication via the same password+TOTP
path the web login uses, so a stolen terminal session alone can't rotate
credentials.
"""
import getpass
import io
import json
import sys

import qrcode

from . import auth, models


def _ascii_qr(data: str) -> str:
    q = qrcode.QRCode(border=1)
    q.add_data(data)
    q.make(fit=True)
    buf = io.StringIO()
    q.print_ascii(out=buf, invert=True)
    return buf.getvalue()


def _prompt_password(label: str = "Password") -> str:
    pw = getpass.getpass(f"{label}: ")
    if not pw:
        print("empty password — aborting.", file=sys.stderr)
        sys.exit(1)
    return pw


def _prompt_new_password() -> str:
    while True:
        p1 = getpass.getpass("New password: ")
        p2 = getpass.getpass("Confirm password: ")
        if p1 != p2:
            print("mismatch — try again.")
            continue
        if len(p1) < 10:
            print("use at least 10 characters.")
            continue
        return p1


def _reauth() -> dict:
    user = models.get_user()
    if user is None:
        print("no user yet — run `init` first.", file=sys.stderr)
        sys.exit(1)
    password = _prompt_password()
    code = input("6-digit code (or recovery code): ").strip()
    try:
        auth.authenticate(password, code)
    except auth.LockoutError as e:
        print(f"account locked until {e.until_iso}.", file=sys.stderr)
        sys.exit(2)
    except auth.AuthError:
        print("invalid credentials.", file=sys.stderr)
        sys.exit(2)
    return models.get_user()


def _print_totp_setup(secret: str) -> None:
    uri = auth.provisioning_uri(secret)
    print()
    print("Scan this QR in your authenticator app (1Password, Google Authenticator, Aegis, ...):")
    print()
    print(_ascii_qr(uri))
    print(f"  Or enter the secret manually: {secret}")
    print(f"  Or open this URI:             {uri}")
    print()


def _print_recovery_codes(codes: list[str]) -> None:
    print()
    print("RECOVERY CODES (save these somewhere safe — they are shown ONCE):")
    for c in codes:
        print(f"  {c}")
    print()
    print("Each code works exactly once. Use one in place of the 6-digit code")
    print("when you can't access your authenticator.")
    print()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_init() -> None:
    models.init_db()
    if models.get_user() is not None:
        print("user already exists — refusing to overwrite.", file=sys.stderr)
        print("use `reset-password` or `rotate-totp` to change credentials.", file=sys.stderr)
        sys.exit(1)
    print("Creating the ephemera sender account.")
    password = _prompt_new_password()
    secret = auth.generate_totp_secret()
    codes, codes_json = auth.generate_recovery_codes()
    models.create_user(
        password_hash=auth.hash_password(password),
        totp_secret=secret,
        recovery_code_hashes=codes_json,
    )
    _print_totp_setup(secret)
    _print_recovery_codes(codes)
    print("Bootstrap complete. You can now sign in at /send.")


def cmd_reset_password() -> None:
    _reauth()
    new_pw = _prompt_new_password()
    models.update_user(password_hash=auth.hash_password(new_pw))
    print("password updated.")


def cmd_rotate_totp() -> None:
    _reauth()
    secret = auth.generate_totp_secret()
    models.update_user(totp_secret=secret, totp_last_step=0)
    _print_totp_setup(secret)
    print("new TOTP active. The old authenticator entry will stop working after you re-scan.")


def cmd_regen_recovery_codes() -> None:
    _reauth()
    codes, codes_json = auth.generate_recovery_codes()
    models.update_user(recovery_code_hashes=codes_json)
    _print_recovery_codes(codes)


def cmd_list_tokens() -> None:
    rows = models.list_tokens()
    if not rows:
        print("(no tokens)")
        return
    for r in rows:
        state = "revoked" if r["revoked_at"] else "active"
        last = r["last_used_at"] or "never"
        print(f"  [{state}] {r['name']}  created {r['created_at']}  last used {last}")


def cmd_create_token(name: str) -> None:
    _reauth()
    plaintext, digest = auth.mint_api_token()
    try:
        models.create_token(name=name, token_hash=digest)
    except Exception as e:
        if "UNIQUE" in str(e):
            print(f"token name '{name}' already exists.", file=sys.stderr)
            sys.exit(1)
        raise
    print()
    print(f"API token '{name}' created. Save this now — it will NOT be shown again:")
    print()
    print(f"  {plaintext}")
    print()
    print("Use as: Authorization: Bearer <token>")


def cmd_revoke_token(name: str) -> None:
    _reauth()
    if models.revoke_token(name):
        print(f"token '{name}' revoked.")
    else:
        print(f"no active token named '{name}'.", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


COMMANDS = {
    "init": (cmd_init, 0),
    "reset-password": (cmd_reset_password, 0),
    "rotate-totp": (cmd_rotate_totp, 0),
    "regen-recovery-codes": (cmd_regen_recovery_codes, 0),
    "list-tokens": (cmd_list_tokens, 0),
    "create-token": (cmd_create_token, 1),
    "revoke-token": (cmd_revoke_token, 1),
}


def main(argv: list[str] | None = None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] not in COMMANDS:
        print(__doc__)
        sys.exit(0 if not argv else 2)
    fn, arity = COMMANDS[argv[0]]
    args = argv[1:]
    if len(args) != arity:
        print(f"`{argv[0]}` expects {arity} positional arg(s).", file=sys.stderr)
        sys.exit(2)
    models.init_db()
    fn(*args)


if __name__ == "__main__":
    main()
