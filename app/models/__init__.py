"""SQLite data layer. Organized as submodules by table:

    app.models._core         -- connection, schema, migrations
    app.models.secrets       -- secrets table (CRUD, reveal, cancel, purge)
    app.models.users         -- users table
    app.models.api_tokens    -- api_tokens table

This `__init__` re-exports the full public (and test-used) surface so existing
`from app import models; models.foo()` call-sites keep working unchanged. New
code is free to import from a specific submodule directly:

    from app.models.secrets import create_secret
    from app.models.users import list_users

Both styles are supported.
"""

from ._core import INDICES_SCRIPT, TABLES_SCRIPT, init_db, ping
from .api_tokens import (
    create_token,
    get_active_token_by_hash,
    list_tokens,
    revoke_token,
    touch_token_last_used,
)
from .secrets import (
    _force_viewed_at,
    burn,
    cancel,
    clear_non_pending_tracked,
    consume_for_reveal,
    create_secret,
    delete_secret,
    get_by_id,
    get_by_token,
    get_status,
    increment_attempts,
    is_expired,
    list_tracked_secrets,
    mark_viewed,
    purge_expired,
    purge_tracked_metadata,
    untrack,
)
from .users import (
    bump_session_generation,
    create_user,
    delete_user,
    get_user_by_id,
    get_user_by_username,
    get_user_with_totp_by_id,
    get_user_with_totp_by_username,
    list_users,
    update_user,
    user_count,
)

__all__ = [
    # _core
    "INDICES_SCRIPT",
    "TABLES_SCRIPT",
    "init_db",
    "ping",
    # secrets
    "burn",
    "cancel",
    "clear_non_pending_tracked",
    "consume_for_reveal",
    "create_secret",
    "delete_secret",
    "get_by_id",
    "get_by_token",
    "get_status",
    "increment_attempts",
    "is_expired",
    "list_tracked_secrets",
    "mark_viewed",
    "purge_expired",
    "purge_tracked_metadata",
    "untrack",
    # users
    "bump_session_generation",
    "create_user",
    "delete_user",
    "get_user_by_id",
    "get_user_by_username",
    "get_user_with_totp_by_id",
    "get_user_with_totp_by_username",
    "list_users",
    "update_user",
    "user_count",
    # api_tokens
    "create_token",
    "get_active_token_by_hash",
    "list_tokens",
    "revoke_token",
    "touch_token_last_used",
    # test helper
    "_force_viewed_at",
]
