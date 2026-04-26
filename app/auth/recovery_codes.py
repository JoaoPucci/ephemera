"""One-time recovery codes: generation, normalization, single-use consumption.

Stored as a JSON list on the user row: each entry is
    {"hash": "<bcrypt>", "used_at": "<ISO8601>" | null}
On consumption, we flip `used_at` on the matching entry and return the new
JSON so the caller can persist it.
"""

import json
import secrets
from datetime import UTC, datetime

import bcrypt

from ._core import BCRYPT_ROUNDS, RECOVERY_CODE_COUNT, RECOVERY_CODE_LENGTH

_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I

# Dummy hash used when an entry in the stored JSON is missing its `hash`
# field or carries a malformed bcrypt string. Paying a dummy-check in those
# branches keeps the per-entry CPU cost uniform, so a timing attacker can't
# distinguish "user has N unused codes, M used, K corrupt" states from each
# other. Precomputed once at import so we don't regenerate on every call.
_DUMMY_BCRYPT_HASH = bcrypt.hashpw(b"dummy", bcrypt.gensalt(rounds=BCRYPT_ROUNDS))


def _random_recovery_code() -> str:
    raw = "".join(
        secrets.choice(_RECOVERY_ALPHABET) for _ in range(RECOVERY_CODE_LENGTH)
    )
    return raw[:5] + "-" + raw[5:]


def generate_recovery_codes() -> tuple[list[str], str]:
    codes = [_random_recovery_code() for _ in range(RECOVERY_CODE_COUNT)]
    hashes = [
        {
            "hash": bcrypt.hashpw(
                c.encode(), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
            ).decode(),
            "used_at": None,
        }
        for c in codes
    ]
    return codes, json.dumps(hashes)


def _normalize_backup_code(code: str) -> str:
    # Hygiene cap before any per-character work. Recovery codes are 11 chars
    # (XXXXX-XXXXX); anything dramatically longer is an attacker probe or a
    # paste accident. Returning "" makes consume_backup_code's bcrypt loop
    # iterate without matching anything, preserving the constant-time
    # iteration invariant (see consume_backup_code's docstring).
    if len(code) > 32:
        return ""
    code = code.strip().upper().replace(" ", "")
    if len(code) == RECOVERY_CODE_LENGTH and "-" not in code:
        code = code[:5] + "-" + code[5:]
    return code


def consume_backup_code(code: str, stored_json: str) -> str | None:
    """Mark the matching code as used and return the updated JSON; or None if
    the code doesn't match / was already used / the JSON is malformed.

    Constant-time over the stored list: bcrypt.checkpw runs once per entry
    regardless of the entry's `used_at` state or whether its stored hash is
    well-formed. Without that invariant, a timing attacker who can submit a
    recovery-code-shaped payload could distinguish between users by how many
    of their 10 codes have been consumed (fewer-unused == fewer bcrypts ==
    faster response). We don't care about early-return speed here -- the
    caller is rate-limited at 10 logins/min/IP and recovery-code login is
    rare in practice.
    """
    code = _normalize_backup_code(code)
    try:
        entries = json.loads(stored_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(entries, list):
        return None
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    matched_index: int | None = None
    code_bytes = code.encode()

    for i, entry in enumerate(entries):
        stored_hash = entry.get("hash") if isinstance(entry, dict) else None
        ok = False
        if isinstance(stored_hash, str) and stored_hash:
            try:
                ok = bcrypt.checkpw(code_bytes, stored_hash.encode())
            except ValueError:
                # Malformed hash parses fast (no HMAC); pay the dummy
                # check so the per-entry cost stays bcrypt-shaped.
                bcrypt.checkpw(code_bytes, _DUMMY_BCRYPT_HASH)
                ok = False
        else:
            # Missing / non-string hash field: pay the dummy cost so the
            # per-entry timing doesn't reveal which slot is corrupt.
            bcrypt.checkpw(code_bytes, _DUMMY_BCRYPT_HASH)

        # First unused match wins. Deliberately don't break -- the remaining
        # entries still get their bcrypt check so the invocation count is
        # constant regardless of where the match lands in the list.
        if ok and matched_index is None and entry.get("used_at") is None:
            matched_index = i

    if matched_index is None:
        return None
    entries[matched_index]["used_at"] = now
    return json.dumps(entries)
