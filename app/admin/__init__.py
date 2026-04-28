"""CLI for user provisioning and token management.

Usage:
    python -m app.admin <command> [args]

The package is organized by concern:

    app.admin._core         -- shared helpers (prompts, user resolution,
                               re-auth, output, audit re-export).
    app.admin.users         -- init, add-user, list-users, remove-user
                               (and the private _reauth_for_force_removal).
    app.admin.rotation      -- reset-password, rotate-totp,
                               regen-recovery-codes.
    app.admin.tokens        -- list-tokens, create-token, revoke-token.
    app.admin.diagnostics   -- diagnose, verify, analytics-summary.
    app.admin.cli           -- COMMANDS dispatch table + main().

This `__init__` re-exports the full surface tests + `from app import
admin` callers have historically used, so existing call sites keep
working unchanged. New code is free to import from a specific submodule:

    from app.admin.users import cmd_init
    from app.admin.tokens import cmd_create_token

Both styles are supported.

Notes for tests: command files import shared helpers as
`from . import _core` and call them as `_core.<helper>(...)`. That
means a `monkeypatch.setattr(admin._core, "_reauth", ...)` propagates
to every command file in the package. Patching `admin._reauth` only
mutates the binding in this `__init__` namespace and does not affect
the command files' lookups.
"""

from . import _core
from ._core import (
    _ascii_qr,
    _parse_user_flag,
    _print_recovery_codes,
    _print_totp_setup,
    _prompt_new_password,
    _prompt_password,
    _provision_user,
    _reauth,
    _resolve_user,
    audit,
)
from .cli import COMMANDS, USAGE_DOC, main
from .diagnostics import cmd_analytics_summary, cmd_diagnose, cmd_verify
from .rotation import cmd_regen_recovery_codes, cmd_reset_password, cmd_rotate_totp
from .tokens import cmd_create_token, cmd_list_tokens, cmd_revoke_token
from .users import (
    _reauth_for_force_removal,
    cmd_add_user,
    cmd_init,
    cmd_list_users,
    cmd_remove_user,
)

__all__ = [
    # ---- Submodule (re-exported so tests can patch via admin._core.xxx) ----
    "_core",
    # ---- Helpers from _core ----
    "_ascii_qr",
    "_parse_user_flag",
    "_print_recovery_codes",
    "_print_totp_setup",
    "_prompt_new_password",
    "_prompt_password",
    "_provision_user",
    "_reauth",
    "_resolve_user",
    "audit",
    # ---- User-lifecycle commands ----
    "cmd_add_user",
    "cmd_init",
    "cmd_list_users",
    "cmd_remove_user",
    "_reauth_for_force_removal",
    # ---- Credential rotation commands ----
    "cmd_regen_recovery_codes",
    "cmd_reset_password",
    "cmd_rotate_totp",
    # ---- API-token commands ----
    "cmd_create_token",
    "cmd_list_tokens",
    "cmd_revoke_token",
    # ---- Diagnostics commands ----
    "cmd_analytics_summary",
    "cmd_diagnose",
    "cmd_verify",
    # ---- Dispatch ----
    "COMMANDS",
    "USAGE_DOC",
    "main",
]
