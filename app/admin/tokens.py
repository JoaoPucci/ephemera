"""API-token commands: list-tokens, create-token, revoke-token.

Tokens are scoped to a user; every command resolves the target user
first via _core._resolve_user. create-token + revoke-token re-auth as
the target before mutating, so shell access alone can't silently mint
or burn another user's tokens.
"""

import sys

from .. import auth, models
from . import _core


def cmd_list_tokens(username: str | None) -> None:
    user = _core._resolve_user(username)
    rows = models.list_tokens(user["id"])
    if not rows:
        print("(no tokens)")
        return
    for r in rows:
        state = "revoked" if r["revoked_at"] else "active"
        last = r["last_used_at"] or "never"
        print(f"  [{state}] {r['name']}  created {r['created_at']}  last used {last}")


def cmd_create_token(name: str, username: str | None) -> None:
    user = _core._resolve_user(username)
    _core._reauth(user)
    plaintext, digest = auth.mint_api_token()
    try:
        models.create_token(user_id=user["id"], name=name, token_hash=digest)
    except Exception as e:
        if "UNIQUE" in str(e):
            print(
                f"token name '{name}' already exists for user '{user['username']}'.",
                file=sys.stderr,
            )
            sys.exit(1)
        raise
    _core.audit(
        "apitoken.created",
        user_id=user["id"],
        username=user["username"],
        token_name=name,
    )
    print()
    print(
        f"API token '{name}' created for user '{user['username']}'. Save this now — it will NOT be shown again:"
    )
    print()
    print(f"  {plaintext}")
    print()
    print("Use as: Authorization: Bearer <token>")


def cmd_revoke_token(name: str, username: str | None) -> None:
    user = _core._resolve_user(username)
    _core._reauth(user)
    if models.revoke_token(user["id"], name):
        _core.audit(
            "apitoken.revoked",
            user_id=user["id"],
            username=user["username"],
            token_name=name,
        )
        print(f"token '{name}' revoked.")
    else:
        print(
            f"no active token named '{name}' for user '{user['username']}'.",
            file=sys.stderr,
        )
        sys.exit(1)
