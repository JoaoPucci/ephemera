"""One-time recovery codes: generation, normalization, single-use consumption.

Stored as a JSON list on the user row: each entry is
    {"hash": "<bcrypt>", "used_at": "<ISO8601>" | null}
On consumption, we flip `used_at` on the matching entry and return the new
JSON so the caller can persist it.
"""
import json
import secrets
from datetime import datetime, timezone
from typing import Optional

import bcrypt

from ._core import BCRYPT_ROUNDS, RECOVERY_CODE_COUNT, RECOVERY_CODE_LENGTH


_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


def _random_recovery_code() -> str:
    raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(RECOVERY_CODE_LENGTH))
    return raw[:5] + "-" + raw[5:]


def generate_recovery_codes() -> tuple[list[str], str]:
    codes = [_random_recovery_code() for _ in range(RECOVERY_CODE_COUNT)]
    hashes = [
        {"hash": bcrypt.hashpw(c.encode(), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode(), "used_at": None}
        for c in codes
    ]
    return codes, json.dumps(hashes)


def _normalize_backup_code(code: str) -> str:
    code = code.strip().upper().replace(" ", "")
    if len(code) == RECOVERY_CODE_LENGTH and "-" not in code:
        code = code[:5] + "-" + code[5:]
    return code


def consume_backup_code(code: str, stored_json: str) -> Optional[str]:
    """Mark the matching code as used and return the updated JSON; or None if
    the code doesn't match / was already used / the JSON is malformed."""
    code = _normalize_backup_code(code)
    try:
        entries = json.loads(stored_json)
    except json.JSONDecodeError:
        return None
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for entry in entries:
        if entry.get("used_at") is not None:
            continue
        try:
            if bcrypt.checkpw(code.encode(), entry["hash"].encode()):
                entry["used_at"] = now
                return json.dumps(entries)
        except ValueError:
            continue
    return None
