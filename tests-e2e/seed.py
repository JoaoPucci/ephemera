"""Seed a fresh ephemera DB with a known user for E2E tests.

Creates a single user with fixed credentials so the Playwright suite can
log in deterministically. The TOTP secret is a well-known RFC 6238 test
vector (same one used in pyotp docs); the matching TOTP codes are
computed in the test with `otplib`.

Wipes any existing DB at EPHEMERA_DB_PATH first so a re-run always
starts from a clean slate.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

if "EPHEMERA_DB_PATH" not in os.environ:
    sys.exit("EPHEMERA_DB_PATH must be set before seeding")

db_path = Path(os.environ["EPHEMERA_DB_PATH"])
for suffix in ("", "-wal", "-shm", "-journal"):
    p = Path(str(db_path) + suffix)
    if p.exists():
        p.unlink()

# Import after the wipe so the models layer can initialise a fresh schema.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.auth import generate_recovery_codes, hash_password  # noqa: E402
from app.models import create_user, init_db  # noqa: E402

PASSWORD = "e2e-password-123"
# 32 base32 chars = 20 bytes = 160 bits, which is the RFC 6238 SHA-1
# recommended size and also clears otplib v13's MIN_SECRET_BYTES=16
# guardrail. Still a synthetic fixed value -- only ever used against the
# throwaway tests-e2e DB on :8765, never a real account's secret.
TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"

# One user per spec file so each test has its own `totp_last_step` row,
# which anti-replay uses to reject re-submissions of an already-spent
# TOTP step. With Playwright's `workers: 1` the suite runs sequentially,
# but multiple specs can land their logins in the same 30-second TOTP
# window -- the second login then computes an identical code, hits the
# anti-replay check, and fails. Per-user isolation removes the cross-test
# coupling cleanly. The keys here are 1:1 with the .spec.js filenames.
USERNAMES = [
    "e2e",
    "e2e-image",
    "e2e-passphrase",
    "e2e-cancel",
    "e2e-expired-secret",
    "e2e-mobile",
]

init_db()
for username in USERNAMES:
    _, recovery_json = generate_recovery_codes()
    create_user(
        username=username,
        password_hash=hash_password(PASSWORD),
        totp_secret=TOTP_SECRET,
        recovery_code_hashes=recovery_json,
        email=None,
    )

print(f"Seeded {', '.join(USERNAMES)} at {db_path}", file=sys.stderr)
