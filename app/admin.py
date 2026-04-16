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
    diagnose                 Print server time + the 3 currently-valid TOTP codes
                             (for debugging "my code keeps failing").
    verify                   Prompt for password + code and say exactly which one
                             (if any) is wrong. UI hides this to prevent enumeration;
                             safe to expose at the CLI since you already have shell.

All "sensitive" commands require re-authentication via the same password+TOTP
path the web login uses, so a stolen terminal session alone can't rotate
credentials.
"""
import getpass
import io
import json
import sys
import time
from datetime import datetime, timezone

import pyotp
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


def cmd_diagnose() -> None:
    """Print the codes a correctly-configured authenticator SHOULD show right now.

    Compare with what your authenticator displays:
      - exact match on "now"    → secret OK, clock OK, code should work
      - match on prev/next      → clock drift up to 30s (still accepted)
      - no match                → authenticator has a stale/different secret
    """
    user = models.get_user()
    if user is None:
        print("no user provisioned yet. run `init` first.", file=sys.stderr)
        sys.exit(1)

    secret = user["totp_secret"]
    totp = pyotp.TOTP(secret, digits=auth.TOTP_DIGITS, interval=auth.TOTP_INTERVAL)

    now_ts = int(time.time())
    step = now_ts // auth.TOTP_INTERVAL
    last_step = int(user.get("totp_last_step") or 0)
    seconds_into = now_ts - step * auth.TOTP_INTERVAL
    seconds_left = auth.TOTP_INTERVAL - seconds_into

    print()
    print(f"Server time (UTC):   {datetime.now(timezone.utc).isoformat()}")
    print(f"Server unix ts:      {now_ts}")
    print(f"Current TOTP step:   {step}  ({seconds_into}s in, {seconds_left}s until next rotation)")
    print(f"Last step used:      {last_step}")
    print()
    print("If your authenticator's current code matches any of these, login will work:")
    print(f"  previous step ({step - 1}):  {totp.at(now_ts - auth.TOTP_INTERVAL)}")
    print(f"  current  step ({step}):      {totp.at(now_ts)}   <-- this is what it should show now")
    print(f"  next     step ({step + 1}):  {totp.at(now_ts + auth.TOTP_INTERVAL)}")
    print()
    print(f"Stored TOTP secret:  {secret}")
    print()
    print(f"Password length (bytes): {len(user['password_hash'])}  (stored bcrypt hash length)")
    print()
    print("If your authenticator shows a different code for the 'current step':")
    print("  -> your authenticator has an OLD entry from a previous `init` / `rotate-totp`.")
    print("     Delete that entry in the authenticator and re-scan the QR from your last")
    print("     `init` or `rotate-totp`. Or run `rotate-totp` to generate a fresh secret + QR.")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def cmd_verify() -> None:
    """Say exactly which factor is wrong. For self-debugging only."""
    user = models.get_user()
    if user is None:
        print("no user provisioned yet. run `init` first.", file=sys.stderr)
        sys.exit(1)

    password = getpass.getpass("Password: ")
    code = input("6-digit code: ").strip()

    pw_ok = auth.verify_password(password, user["password_hash"])
    # Evaluate TOTP without mutating totp_last_step, so we can re-test freely.
    totp_step = None
    if code.isdigit() and len(code) == auth.TOTP_DIGITS:
        totp_step = auth.verify_totp(user["totp_secret"], code, last_step=user["totp_last_step"])

    print()
    print(f"password:  {'OK' if pw_ok else 'MISMATCH'}")
    print(f"totp:      {'OK (step ' + str(totp_step) + ')' if totp_step is not None else 'MISMATCH'}")
    print(f"stored totp_last_step: {user['totp_last_step']}")
    print()

    if not pw_ok and totp_step is None:
        print("Both password and TOTP are wrong. If you've forgotten the password,")
        print("wipe the DB and run `init` again (no real secrets exist yet).")
    elif not pw_ok:
        print("Password is wrong; TOTP is correct.")
        print("If you've forgotten the password, wipe the DB and run `init` again.")
    elif totp_step is None:
        print("Password is right; TOTP is wrong.")
        print("Most likely: clock drift or a stale authenticator entry.")
        print("Run `rotate-totp` to regenerate the secret and rescan the QR.")
    else:
        print("Both correct. Login via the web UI should succeed with these values.")
        print("If the UI still rejects them, check: (a) you are sending to the same")
        print("server you diagnosed against, and (b) the DB path (EPHEMERA_DB_PATH)")
        print("used by the server matches the one this CLI is using.")


COMMANDS = {
    "init": (cmd_init, 0),
    "reset-password": (cmd_reset_password, 0),
    "rotate-totp": (cmd_rotate_totp, 0),
    "regen-recovery-codes": (cmd_regen_recovery_codes, 0),
    "list-tokens": (cmd_list_tokens, 0),
    "create-token": (cmd_create_token, 1),
    "revoke-token": (cmd_revoke_token, 1),
    "diagnose": (cmd_diagnose, 0),
    "verify": (cmd_verify, 0),
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
