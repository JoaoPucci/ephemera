#!/usr/bin/env bash
# Playwright's webServer entrypoint. Wipes + seeds a fresh DB, then execs
# uvicorn on a test port so the suite never collides with a dev server
# running on :8000. Env var values are deterministic so the test can log
# in with known credentials.
set -euo pipefail
cd "$(dirname "$0")/.."

export EPHEMERA_DB_PATH="$(pwd)/ephemera-e2e.db"
export EPHEMERA_SECRET_KEY="e2e-smoke-test-secret-key-at-least-32-chars-long-aaaaaa"
export EPHEMERA_BASE_URL="http://127.0.0.1:8765"
export EPHEMERA_ALLOWED_ORIGINS="http://127.0.0.1:8765"

./venv/bin/python tests-e2e/seed.py
exec ./venv/bin/uvicorn app:create_app \
  --factory \
  --host 127.0.0.1 \
  --port 8765
