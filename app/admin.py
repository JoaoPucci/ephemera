"""CLI for user provisioning and token management.

Usage:
    python -m app.admin <command> [args]

Commands:
    init <username>                  First-time setup: create the initial user.
    add-user <username>              Provision another user (requires re-auth).
    list-users                       Show all users.
    remove-user <username> [--force] Delete a user (and all their data).
                                     Default: re-auth as <username>.
                                     --force: re-auth as any OTHER user
                                     (for when the target's creds are lost).
    reset-password [--user <name>]   Change password for a user (default: yourself).
    rotate-totp [--user <name>]      Generate a new TOTP secret.
    regen-recovery-codes [--user <name>]  Print 10 fresh recovery codes.
    list-tokens [--user <name>]      Show API tokens for a user.
    create-token <name> [--user <u>] Mint a new API token.
    revoke-token <name> [--user <u>] Revoke a token by name.
    diagnose [--user <name>] [--show-secret]
                                     Print server time + currently-valid TOTP codes.
                                     --show-secret also prints the raw TOTP seed
                                     (only needed if you're re-entering it in an
                                     authenticator app by hand; default omits it
                                     so terminal scrollback / screen-share / chat
                                     paste don't carry the seed for routine checks).
    verify [--user <name>]           Check whether a password+code pair would authenticate.

User-selection rules:
- Commands without a positional user and no --user flag default to the sole
  user if there is exactly one (single-user convenience), otherwise they ask.
- Sensitive commands re-authenticate against the target user before mutating.
"""
import getpass
import io
import json
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import pyotp
import qrcode

from . import auth, models
from .config import get_settings
from .security_log import emit as audit


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
    from .auth.hibp import pwned_count

    while True:
        p1 = getpass.getpass("New password: ")
        p2 = getpass.getpass("Confirm password: ")
        if p1 != p2:
            print("mismatch — try again.")
            continue
        if len(p1) < 10:
            print("use at least 10 characters.")
            continue
        count = pwned_count(p1)
        if count is None:
            # API unreachable (offline host / DNS blip). Degrade to a loud
            # warning rather than blocking password setup entirely.
            print("warning: couldn't reach the breach-check API; skipping pwned check.")
            return p1
        if count > 0:
            print(
                f"this password appears in {count:,} known breaches. "
                "pick a different one."
            )
            continue
        return p1


def _parse_user_flag(args: list[str]) -> tuple[Optional[str], list[str]]:
    """Extract '--user <name>' (or '-u <name>') from args, return (name, remaining)."""
    out = []
    i = 0
    username: Optional[str] = None
    while i < len(args):
        a = args[i]
        if a in ("--user", "-u") and i + 1 < len(args):
            username = args[i + 1]
            i += 2
            continue
        out.append(a)
        i += 1
    return username, out


def _resolve_user(username: Optional[str]) -> dict:
    """Pick the target user: explicit flag, or the only user, or prompt."""
    if username:
        user = models.get_user_by_username(username)
        if user is None:
            print(f"no user named '{username}'.", file=sys.stderr)
            sys.exit(1)
        return user
    users = models.list_users()
    if len(users) == 0:
        print("no users yet — run `init <username>` first.", file=sys.stderr)
        sys.exit(1)
    if len(users) == 1:
        return models.get_user_by_id(users[0]["id"])
    # Multiple users, no flag. Ask.
    print("Multiple users exist; specify --user <name>. Known users:", file=sys.stderr)
    for u in users:
        print(f"  {u['username']}", file=sys.stderr)
    sys.exit(1)


def _reauth(user: dict) -> dict:
    """Re-authenticate as `user` before any sensitive action."""
    password = _prompt_password()
    code = input("6-digit code (or recovery code): ").strip()
    try:
        return auth.authenticate(user["username"], password, code)
    except auth.LockoutError as e:
        print(f"account locked until {e.until_iso}.", file=sys.stderr)
        sys.exit(2)
    except auth.AuthError:
        print("invalid credentials.", file=sys.stderr)
        sys.exit(2)


def _print_totp_setup(secret: str, username: str) -> None:
    uri = auth.provisioning_uri(
        secret, account_name=username, issuer=get_settings().totp_issuer
    )
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


def _provision_user(username: str) -> tuple[int, str, list[str]]:
    """Shared bootstrap for `init` and `add-user`: returns (user_id, totp_secret, recovery_codes)."""
    if models.get_user_by_username(username):
        print(f"username '{username}' already exists.", file=sys.stderr)
        sys.exit(1)
    print(f"Creating user '{username}'.")
    password = _prompt_new_password()
    secret = auth.generate_totp_secret()
    codes, codes_json = auth.generate_recovery_codes()
    uid = models.create_user(
        username=username,
        password_hash=auth.hash_password(password),
        totp_secret=secret,
        recovery_code_hashes=codes_json,
    )
    audit("user.added", user_id=uid, username=username)
    return uid, secret, codes


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_init(username: str) -> None:
    models.init_db()
    if models.user_count() > 0:
        print("at least one user already exists — refusing to run init.", file=sys.stderr)
        print("use `add-user` for additional users, or rotation commands to change credentials.", file=sys.stderr)
        sys.exit(1)
    _, secret, codes = _provision_user(username)
    _print_totp_setup(secret, username)
    _print_recovery_codes(codes)
    print("Bootstrap complete. You can now sign in at /send.")


def cmd_add_user(username: str) -> None:
    # Require re-auth as an existing user to prevent anyone with shell-less
    # elevated SQLite access from silently minting friends' accounts.
    existing = models.list_users()
    if not existing:
        print("no users yet — run `init <username>` first.", file=sys.stderr)
        sys.exit(1)
    # Re-auth as whichever user the caller prefers (or the sole one).
    actor = _resolve_user(None)
    print(f"Re-authenticate as '{actor['username']}' to add a new user.")
    _reauth(actor)
    _, secret, codes = _provision_user(username)
    _print_totp_setup(secret, username)
    _print_recovery_codes(codes)
    print(f"User '{username}' created.")


def cmd_list_users() -> None:
    users = models.list_users()
    if not users:
        print("(no users)")
        return
    for u in users:
        print(f"  {u['id']:>3}  {u['username']:<20}  created {u['created_at']}")


def cmd_remove_user(username: str, force: bool = False) -> None:
    target = models.get_user_by_username(username)
    if target is None:
        print(f"no user named '{username}'.", file=sys.stderr)
        sys.exit(1)
    if models.user_count() == 1:
        print("refusing to remove the only remaining user.", file=sys.stderr)
        sys.exit(1)

    if force:
        # Shell + another account's creds are the bar. Lets you remove a user
        # whose password/TOTP/recovery codes you no longer have, without
        # dropping to raw SQL.
        _reauth_for_force_removal(target)
    else:
        # Normal mode: target authenticates their own removal (same bar as a
        # password rotation -- they must have their own creds).
        print(f"Re-authenticate as '{username}' to confirm removal.")
        _reauth(target)

    models.delete_user(target["id"])
    audit("user.removed", user_id=target["id"], username=username, forced=force)
    print(f"User '{username}' and all their data deleted.")


def _reauth_for_force_removal(target: dict) -> None:
    """Re-auth as any user OTHER than the one being removed."""
    print(f"Force mode. Re-authenticate as a user OTHER than '{target['username']}':")
    auth_username = input("Your username: ").strip()
    if not auth_username:
        print("empty username — aborting.", file=sys.stderr)
        sys.exit(1)
    if auth_username == target["username"]:
        print(
            f"Force mode needs a different user. "
            f"To remove '{target['username']}' as themselves, drop --force.",
            file=sys.stderr,
        )
        sys.exit(1)
    authenticator = models.get_user_by_username(auth_username)
    if authenticator is None:
        print(f"no user named '{auth_username}'.", file=sys.stderr)
        sys.exit(1)
    _reauth(authenticator)


def cmd_reset_password(username: Optional[str]) -> None:
    user = _resolve_user(username)
    _reauth(user)
    new_pw = _prompt_new_password()
    models.update_user(user["id"], password_hash=auth.hash_password(new_pw))
    models.bump_session_generation(user["id"])
    audit("password.reset", user_id=user["id"], username=user["username"])
    print(f"password updated for '{user['username']}'.")
    print("  live sessions for this user have been invalidated; they must log in again.")


def cmd_rotate_totp(username: Optional[str]) -> None:
    user = _resolve_user(username)
    _reauth(user)
    secret = auth.generate_totp_secret()
    models.update_user(user["id"], totp_secret=secret, totp_last_step=0)
    models.bump_session_generation(user["id"])
    audit("totp.rotated", user_id=user["id"], username=user["username"])
    _print_totp_setup(secret, user["username"])
    print("new TOTP active. The old authenticator entry will stop working after you re-scan.")
    print("  live sessions for this user have been invalidated; they must log in again.")


def cmd_regen_recovery_codes(username: Optional[str]) -> None:
    user = _resolve_user(username)
    _reauth(user)
    codes, codes_json = auth.generate_recovery_codes()
    models.update_user(user["id"], recovery_code_hashes=codes_json)
    models.bump_session_generation(user["id"])
    audit("recovery.regenerated", user_id=user["id"], username=user["username"])
    _print_recovery_codes(codes)
    print("  live sessions for this user have been invalidated; they must log in again.")


def cmd_list_tokens(username: Optional[str]) -> None:
    user = _resolve_user(username)
    rows = models.list_tokens(user["id"])
    if not rows:
        print("(no tokens)")
        return
    for r in rows:
        state = "revoked" if r["revoked_at"] else "active"
        last = r["last_used_at"] or "never"
        print(f"  [{state}] {r['name']}  created {r['created_at']}  last used {last}")


def cmd_create_token(name: str, username: Optional[str]) -> None:
    user = _resolve_user(username)
    _reauth(user)
    plaintext, digest = auth.mint_api_token()
    try:
        models.create_token(user_id=user["id"], name=name, token_hash=digest)
    except Exception as e:
        if "UNIQUE" in str(e):
            print(f"token name '{name}' already exists for user '{user['username']}'.", file=sys.stderr)
            sys.exit(1)
        raise
    audit("apitoken.created", user_id=user["id"], username=user["username"], token_name=name)
    print()
    print(f"API token '{name}' created for user '{user['username']}'. Save this now — it will NOT be shown again:")
    print()
    print(f"  {plaintext}")
    print()
    print("Use as: Authorization: Bearer <token>")


def cmd_revoke_token(name: str, username: Optional[str]) -> None:
    user = _resolve_user(username)
    _reauth(user)
    if models.revoke_token(user["id"], name):
        audit("apitoken.revoked", user_id=user["id"], username=user["username"], token_name=name)
        print(f"token '{name}' revoked.")
    else:
        print(f"no active token named '{name}' for user '{user['username']}'.", file=sys.stderr)
        sys.exit(1)


def cmd_diagnose(username: Optional[str], show_secret: bool = False) -> None:
    # `_resolve_user` returns without TOTP plaintext (module default).
    # This command generates candidate TOTP codes from the seed, so
    # explicitly re-fetch the with-TOTP variant.
    resolved = _resolve_user(username)
    user = models.get_user_with_totp_by_id(resolved["id"])
    secret = user["totp_secret"]
    totp = pyotp.TOTP(secret, digits=auth.TOTP_DIGITS, interval=auth.TOTP_INTERVAL)

    now_ts = int(time.time())
    step = now_ts // auth.TOTP_INTERVAL
    last_step = int(user.get("totp_last_step") or 0)
    seconds_into = now_ts - step * auth.TOTP_INTERVAL
    seconds_left = auth.TOTP_INTERVAL - seconds_into

    print()
    print(f"User:                '{user['username']}' (id={user['id']})")
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
    # The raw TOTP seed is gated behind --show-secret. The common reason
    # to run `diagnose` is clock drift -- the three candidate codes above
    # answer that question. The raw seed is only needed for the rare
    # "re-enter my authenticator entry by hand" case, and having it on
    # the terminal by default means tmux scrollback, screen-share demos,
    # and accidental paste-into-chat all carry the seed for the common
    # case too.
    if show_secret:
        print("  !! DO NOT paste, screenshot, or share the line below.")
        print("  !! It is equivalent to a password + 2FA combined.")
        print(f"Stored TOTP secret:  {secret}")
    else:
        print(
            "(Stored TOTP secret not shown. If you need to re-enter it in your "
            "authenticator manually, rerun with `--show-secret`.)"
        )
    print()
    print("If your authenticator shows a different code for the 'current step':")
    print("  -> your authenticator has an OLD entry from a previous `init` / `rotate-totp`.")
    print("     Delete that entry in the authenticator and re-scan the QR from your last")
    print("     `init` or `rotate-totp`. Or run `rotate-totp` to generate a fresh secret + QR.")


def cmd_verify(username: Optional[str]) -> None:
    # Verifies TOTP against the stored seed -- fetch the with-TOTP variant.
    resolved = _resolve_user(username)
    user = models.get_user_with_totp_by_id(resolved["id"])
    password = getpass.getpass("Password: ")
    code = input("6-digit code: ").strip()

    pw_ok = auth.verify_password(password, user["password_hash"])
    totp_step = None
    if code.isdigit() and len(code) == auth.TOTP_DIGITS:
        totp_step = auth.verify_totp(user["totp_secret"], code, last_step=user["totp_last_step"])

    print()
    print(f"user:      '{user['username']}' (id={user['id']})")
    print(f"password:  {'OK' if pw_ok else 'MISMATCH'}")
    print(f"totp:      {'OK (step ' + str(totp_step) + ')' if totp_step is not None else 'MISMATCH'}")
    print(f"stored totp_last_step: {user['totp_last_step']}")
    print()

    if not pw_ok and totp_step is None:
        print("Both password and TOTP are wrong.")
    elif not pw_ok:
        print("Password is wrong; TOTP is correct.")
    elif totp_step is None:
        print("Password is right; TOTP is wrong (clock drift or stale authenticator entry).")
    else:
        print("Both correct. Login via the web UI should succeed with these values.")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


COMMANDS = {
    # name: (fn, positional_arity, takes_user_flag)
    "init":                 (cmd_init,                 1, False),
    "add-user":             (cmd_add_user,             1, False),
    "list-users":           (cmd_list_users,           0, False),
    "remove-user":          (cmd_remove_user,          1, False),
    "reset-password":       (cmd_reset_password,       0, True),
    "rotate-totp":          (cmd_rotate_totp,          0, True),
    "regen-recovery-codes": (cmd_regen_recovery_codes, 0, True),
    "list-tokens":          (cmd_list_tokens,          0, True),
    "create-token":         (cmd_create_token,         1, True),
    "revoke-token":         (cmd_revoke_token,         1, True),
    "diagnose":             (cmd_diagnose,             0, True),
    "verify":               (cmd_verify,               0, True),
}


def main(argv: list[str] | None = None) -> None:
    # Security events are visible via the handler that security_log installs
    # on the `ephemera` parent logger on import -- no basicConfig needed here.
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] not in COMMANDS:
        print(__doc__)
        sys.exit(0 if not argv else 2)
    fn, arity, takes_user = COMMANDS[argv[0]]
    rest = argv[1:]
    # Some commands take optional boolean flags that we strip here before
    # the arity check so they don't count as positional arguments.
    extra_kwargs: dict = {}
    if argv[0] == "remove-user" and "--force" in rest:
        rest = [a for a in rest if a != "--force"]
        extra_kwargs["force"] = True
    if argv[0] == "diagnose" and "--show-secret" in rest:
        rest = [a for a in rest if a != "--show-secret"]
        extra_kwargs["show_secret"] = True
    user_flag, rest = (_parse_user_flag(rest) if takes_user else (None, rest))
    if len(rest) != arity:
        print(f"`{argv[0]}` expects {arity} positional arg(s).", file=sys.stderr)
        sys.exit(2)
    models.init_db()
    if takes_user:
        fn(*rest, user_flag, **extra_kwargs)
    else:
        fn(*rest, **extra_kwargs)


if __name__ == "__main__":
    main()
