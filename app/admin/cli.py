"""Command dispatch table + argv parser. The single thing the
`python -m app.admin <name> ...` entry point reaches into."""

import sys
from collections.abc import Callable
from typing import Any

from .. import models
from . import _core
from .diagnostics import cmd_analytics_summary, cmd_diagnose, cmd_verify
from .rotation import cmd_regen_recovery_codes, cmd_reset_password, cmd_rotate_totp
from .tokens import cmd_create_token, cmd_list_tokens, cmd_revoke_token
from .users import cmd_add_user, cmd_init, cmd_list_users, cmd_remove_user

# Module docstring shown on bare `python -m app.admin`. Lives here (rather
# than in __init__.py) so editing the help text doesn't ripple through
# every importer of the package.
USAGE_DOC = """\
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
    analytics-summary <event_type>   Print count + p50/p95/p99 of int payload fields
                                     for events of <event_type>. Read-only over the
                                     analytics_events table.

User-selection rules:
- Commands without a positional user and no --user flag default to the sole
  user if there is exactly one (single-user convenience), otherwise they ask.
- Sensitive commands re-authenticate against the target user before mutating.
"""

# Each value is `(fn, positional_arity, takes_user_flag)`. The function
# signatures vary across commands (some take `username`, some take
# `event_type`, some take a trailing `--user` resolved at dispatch time);
# `Callable[..., None]` accepts the heterogeneous shape so mypy doesn't
# choke on the `fn(*rest, ...)` call below.
COMMANDS: dict[str, tuple[Callable[..., None], int, bool]] = {
    # name: (fn, positional_arity, takes_user_flag)
    "init": (cmd_init, 1, False),
    "add-user": (cmd_add_user, 1, False),
    "list-users": (cmd_list_users, 0, False),
    "remove-user": (cmd_remove_user, 1, False),
    "reset-password": (cmd_reset_password, 0, True),
    "rotate-totp": (cmd_rotate_totp, 0, True),
    "regen-recovery-codes": (cmd_regen_recovery_codes, 0, True),
    "list-tokens": (cmd_list_tokens, 0, True),
    "create-token": (cmd_create_token, 1, True),
    "revoke-token": (cmd_revoke_token, 1, True),
    "diagnose": (cmd_diagnose, 0, True),
    "verify": (cmd_verify, 0, True),
    "analytics-summary": (cmd_analytics_summary, 1, False),
}


def main(argv: list[str] | None = None) -> None:
    # Security events are visible via the handler that security_log installs
    # on the `ephemera` parent logger on import -- no basicConfig needed here.
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] not in COMMANDS:
        print(USAGE_DOC)
        sys.exit(0 if not argv else 2)
    fn, arity, takes_user = COMMANDS[argv[0]]
    rest = argv[1:]
    # Some commands take optional boolean flags that we strip here before
    # the arity check so they don't count as positional arguments.
    extra_kwargs: dict[str, Any] = {}
    if argv[0] == "remove-user" and "--force" in rest:
        rest = [a for a in rest if a != "--force"]
        extra_kwargs["force"] = True
    if argv[0] == "diagnose" and "--show-secret" in rest:
        rest = [a for a in rest if a != "--show-secret"]
        extra_kwargs["show_secret"] = True
    user_flag, rest = _core._parse_user_flag(rest) if takes_user else (None, rest)
    if len(rest) != arity:
        print(f"`{argv[0]}` expects {arity} positional arg(s).", file=sys.stderr)
        sys.exit(2)
    models.init_db()
    if takes_user:
        fn(*rest, user_flag, **extra_kwargs)
    else:
        fn(*rest, **extra_kwargs)
