"""Shared CLI primitives: prompts, user resolution, re-auth, output helpers,
and the structured-audit alias.

The command modules in this package (`users`, `rotation`, `tokens`,
`diagnostics`) all import this module as `from . import _core` and call
helpers as `_core.<name>(...)`. That convention is load-bearing for tests:
patching `app.admin._core.<helper>` propagates to every command file that
reaches for it via attribute access. A direct `from ._core import <name>`
in a command file would create a per-module local binding that test
monkeypatching wouldn't reach.
"""

import getpass
import io
import sys
from typing import Any

import qrcode

from .. import auth, models
from ..config import get_settings
from ..security_log import emit

# Re-export under the `audit` name. Command modules and tests reach
# for `_core.audit(...)` so monkeypatching one symbol intercepts every
# call site at once. Plain assignment (rather than `import emit as
# audit`) is mypy's recognised re-export shape under `strict = true`.
audit = emit


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
    from ..auth.hibp import pwned_count

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


def _parse_user_flag(args: list[str]) -> tuple[str | None, list[str]]:
    """Extract '--user <name>' (or '-u <name>') from args, return (name, remaining)."""
    out = []
    i = 0
    username: str | None = None
    while i < len(args):
        a = args[i]
        if a in ("--user", "-u") and i + 1 < len(args):
            username = args[i + 1]
            i += 2
            continue
        out.append(a)
        i += 1
    return username, out


def _resolve_user(username: str | None) -> dict[str, Any]:
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
        # `get_user_by_id` is annotated `dict | None` but the row we just
        # got from `list_users()` is the same one we're looking up, so the
        # None branch is unreachable here. The assert documents the
        # invariant and gives mypy the narrowing it needs.
        only = models.get_user_by_id(users[0]["id"])
        assert only is not None
        return only
    # Multiple users, no flag. Ask.
    print("Multiple users exist; specify --user <name>. Known users:", file=sys.stderr)
    for u in users:
        print(f"  {u['username']}", file=sys.stderr)
    sys.exit(1)


def _reauth(user: dict[str, Any]) -> None:
    """Re-authenticate as `user` before any sensitive action.

    Returns on success, exits non-zero on failure. No return value --
    callers don't need the authenticated user dict (they already have
    the one `_resolve_user` handed them), and holding a reference to
    `authenticate()`'s return value would also re-surface the plaintext-
    seed-in-memory shape that the models-layer split was designed to
    avoid.
    """
    password = _prompt_password()
    code = input("6-digit code (or recovery code): ").strip()
    try:
        auth.authenticate(user["username"], password, code)
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
    print(
        "Scan this QR in your authenticator app (1Password, Google Authenticator, Aegis, ...):"
    )
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
