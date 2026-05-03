"""User-lifecycle commands: init, add-user, list-users, remove-user.

remove-user has two modes -- normal (target re-auths as themselves) and
force (a different user re-auths to bless the removal). The force-mode
helper is private to this module: no other command needs it.
"""

import sys
from typing import Any

from .. import models
from . import _core


def cmd_init(username: str) -> None:
    models.init_db()
    if models.user_count() > 0:
        print(
            "at least one user already exists — refusing to run init.", file=sys.stderr
        )
        print(
            "use `add-user` for additional users, or rotation commands to change credentials.",
            file=sys.stderr,
        )
        sys.exit(1)
    _, secret, codes = _core._provision_user(username)
    _core._print_totp_setup(secret, username)
    _core._print_recovery_codes(codes)
    print("Bootstrap complete. You can now sign in at /send.")


def cmd_add_user(username: str) -> None:
    # Require re-auth as an existing user to prevent anyone with shell-less
    # elevated SQLite access from silently minting friends' accounts.
    existing = models.list_users()
    if not existing:
        print("no users yet — run `init <username>` first.", file=sys.stderr)
        sys.exit(1)
    # Re-auth as whichever user the caller prefers (or the sole one).
    actor = _core._resolve_user(None)
    print(f"Re-authenticate as '{actor['username']}' to add a new user.")
    _core._reauth(actor)
    _, secret, codes = _core._provision_user(username)
    _core._print_totp_setup(secret, username)
    _core._print_recovery_codes(codes)
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
        _core._reauth(target)

    models.delete_user(target["id"])
    _core.audit("user.removed", user_id=target["id"], username=username, forced=force)
    print(f"User '{username}' and all their data deleted.")


def _reauth_for_force_removal(target: dict[str, Any]) -> None:
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
    _core._reauth(authenticator)
