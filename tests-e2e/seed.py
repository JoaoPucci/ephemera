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

USERNAME = "e2e"
PASSWORD = "e2e-password-123"
TOTP_SECRET = "JBSWY3DPEHPK3PXP"

init_db()
_, recovery_json = generate_recovery_codes()
create_user(
    username=USERNAME,
    password_hash=hash_password(PASSWORD),
    totp_secret=TOTP_SECRET,
    recovery_code_hashes=recovery_json,
    email=None,
)

print(f"Seeded {USERNAME} at {db_path}", file=sys.stderr)
