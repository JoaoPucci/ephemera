#!/usr/bin/env bash
# Playwright's webServer entrypoint. Wipes + seeds a fresh DB, then execs
# uvicorn on a test port so the suite never collides with a dev server
# running on :8000. Env var values are deterministic so the test can log
# in with known credentials.
#
# DB lives under tests-e2e/.tmp/ rather than repo root: keeps test-only
# state inside the test harness's own directory, mirrors how dev state
# now lives under ~/.local/share/ephemera-dev/, and removes the one
# exception to "no DB files at the repo root."
set -euo pipefail
cd "$(dirname "$0")/.."

TMP_DIR="$(pwd)/tests-e2e/.tmp"
mkdir -p "$TMP_DIR"

export EPHEMERA_DB_PATH="$TMP_DIR/ephemera-e2e.db"
export EPHEMERA_SECRET_KEY="e2e-smoke-test-secret-key-at-least-32-chars-long-aaaaaa"
export EPHEMERA_BASE_URL="http://127.0.0.1:8765"
export EPHEMERA_ALLOWED_ORIGINS="http://127.0.0.1:8765"

./venv/bin/python tests-e2e/seed.py
exec ./venv/bin/uvicorn app:create_app \
  --factory \
  --host 127.0.0.1 \
  --port 8765
