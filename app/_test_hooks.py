"""E2E-test-only HTTP hooks.

Mounts under `/_test/*` and is registered into the app ONLY when the
`EPHEMERA_E2E_TEST_HOOKS=1` environment variable is set -- production
deploys don't set it, so these routes don't exist on the wire. The env
var name carries "TEST" and "E2E" prominently for deployment-config
audit visibility (mirrors the `EPHEMERA_TEST_BCRYPT_ROUNDS_OVERRIDE`
pattern from app/auth/_core.py).

Two endpoints today, intentionally narrow:

  POST /_test/limiter/reset
      Clears in-memory rate-limiter state. The Playwright suite calls
      this in `beforeEach` so cross-test pollution doesn't trip a 429
      in unrelated specs. Mirrors what the pytest `client` fixture
      does (in tests/conftest.py) for backend tests.

  POST /_test/secret/{token}/expire-now
      UPDATEs the secret's `expires_at` column to a past timestamp so
      the next reveal lookup classifies it as expired. The expired-
      secret e2e spec uses this instead of a global clock-fast-
      forward (which would also affect the limiter and TOTP
      verification, complicating the test).

Adding more endpoints here is fine, but each one must be the smallest
surface needed for an e2e spec -- this module is not a back door for
arbitrary test-time mutation. If a spec needs broader app-state
manipulation, prefer driving the public API or extending an existing
spec rather than growing this surface.
"""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException

from .dependencies import verify_same_origin
from .limiter import (
    create_limiter,
    login_limiter,
    read_limiter,
    read_rate_limit,
    reveal_limiter,
)
from .models._core import _connect

# Both routes carry `Depends(verify_same_origin)` so the
# `test_state_mutating_routes_all_carry_origin_gate` fitness invariant
# stays universal -- no allowlist carve-outs for "test-only" surfaces.
# Both also carry `Depends(read_rate_limit)` for the same reason against
# `test_state_mutating_routes_all_carry_rate_limiter`. The CSRF concern
# still applies in test mode (a hostile page in a parallel browser
# context could otherwise reset the limiter mid-spec or expire a
# tracked secret), Playwright's `request` fixture sends the Origin
# header explicitly per spec, and the read_rate_limit budget (300/min)
# is comfortably above the handful of hits the e2e suite ever issues
# against these endpoints, so wiring both deps up costs nothing
# operationally.
router = APIRouter(prefix="/_test", include_in_schema=False)


@router.post(
    "/limiter/reset",
    dependencies=[Depends(read_rate_limit), Depends(verify_same_origin)],
)
def reset_limiters() -> dict:
    """Clear all in-memory rate-limiter state."""
    for lim in (reveal_limiter, login_limiter, create_limiter, read_limiter):
        lim.reset()
    return {"ok": True}


@router.post(
    "/secret/{token}/expire-now",
    dependencies=[Depends(read_rate_limit), Depends(verify_same_origin)],
)
def expire_secret_now(token: str) -> dict:
    """Force a secret's `expires_at` to a past timestamp so the next
    reveal lookup sees it as expired. Returns 404 if no row matches
    the token -- avoids a silent no-op when the spec misnames a
    token (which would otherwise look like the assertion failed for
    an unrelated reason)."""
    past = (datetime.now(UTC) - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE secrets SET expires_at = ? WHERE token = ?",
            (past, token),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="secret not found")
    return {"ok": True}
