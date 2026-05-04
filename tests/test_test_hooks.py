"""Unit tests for the e2e-only `/_test/*` router.

The router lives in `app/_test_hooks.py` and is registered into
`create_app()` only when `EPHEMERA_E2E_TEST_HOOKS=1`. The
`tests-e2e/start.sh` flow exercises it through Playwright; these tests
pin the same behaviour at the pytest level so:

  - the env-gated registration in `app/__init__.py` has direct
    coverage of both branches (registered / not-registered);
  - the two endpoints are behaviour-locked against unintentional
    drift that the e2e suite would only catch via a slower red run;
  - mutations on the router itself (if `app/_test_hooks.py` ever
    joins the cosmic-ray scope) have a fast-killing test surface.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import create_app
from app.limiter import create_limiter, login_limiter, read_limiter, reveal_limiter


@pytest.fixture
def hooks_client(tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """TestClient with `EPHEMERA_E2E_TEST_HOOKS=1` set BEFORE
    `create_app()` runs, so the `/_test/*` router gets registered.
    Resets all limiters before AND after the test (the limiters are
    module-level singletons; reuse across tests would pollute state).

    Settings cache is cleared AFTER the env-var setdefault because
    `tmp_db_path` already populated the lru_cache with a settings
    instance that didn't have `e2e_test_hooks` set; without the
    second clear, pydantic-settings would hand back the stale
    cached snapshot and `create_app()` would skip the test-hooks
    registration.
    """
    monkeypatch.setenv("EPHEMERA_E2E_TEST_HOOKS", "1")

    from app import config

    config.get_settings.cache_clear()
    for lim in (reveal_limiter, login_limiter, create_limiter, read_limiter):
        lim.reset()
    app = create_app()
    with TestClient(app) as c:
        yield c
    for lim in (reveal_limiter, login_limiter, create_limiter, read_limiter):
        lim.reset()


def test_test_hooks_router_not_registered_in_default_pytest_mode(client: TestClient) -> None:
    """The default `client` fixture doesn't set
    `EPHEMERA_E2E_TEST_HOOKS`, so the /_test/* routes return 404.
    Pins the production posture -- prod deploys never set the env
    var, so the routes don't exist on the wire and a probe gets the
    same 404 it would for any unmounted path. Catches a regression
    that drops the env-gating and registers the router
    unconditionally."""
    r = client.post("/_test/limiter/reset", headers={"Origin": "http://testserver"})
    assert r.status_code == 404


def test_limiter_reset_endpoint_clears_in_memory_state(hooks_client: TestClient) -> None:
    """POST /_test/limiter/reset returns 200 and the limiter's
    `_hits` dict is empty afterwards. Burns one hit on each named
    limiter to ensure pre-state isn't already empty (otherwise the
    assertion would silently pass on a no-op endpoint)."""
    reveal_limiter.check("1.2.3.4")
    login_limiter.check("1.2.3.4")
    create_limiter.check("session-id-xyz")
    read_limiter.check("1.2.3.4")
    assert reveal_limiter._hits, "preconditions: pre-state not empty"

    r = hooks_client.post(
        "/_test/limiter/reset", headers={"Origin": "http://testserver"}
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert reveal_limiter._hits == {}
    assert login_limiter._hits == {}
    assert create_limiter._hits == {}
    assert read_limiter._hits == {}


def test_expire_secret_now_flips_expires_at_into_past(
    hooks_client: TestClient, auth_headers: dict[str, str]
) -> None:
    """POST /_test/secret/{token}/expire-now updates the matching
    secret's `expires_at` column to a past timestamp so the next
    reveal lookup classifies it as expired. Pins the SQL behaviour
    end-to-end (handler + UPDATE + the actual column write)."""
    create = hooks_client.post(
        "/api/secrets",
        json={"content": "expire-me", "content_type": "text", "expires_in": 3600},
        headers=auth_headers,
    )
    assert create.status_code == 201, create.text
    url = create.json()["url"]
    token = url.rsplit("/", 1)[-1].split("#", 1)[0]

    r = hooks_client.post(
        f"/_test/secret/{token}/expire-now",
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    from app import models

    row = models.get_by_token(token)
    assert row is not None
    expires_at = datetime.strptime(row["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    )
    # The handler subtracts 60s from `now`; should land in the past
    # by at least the test's wall-clock delta plus that 60s.
    assert expires_at < datetime.now(UTC) - timedelta(seconds=10)


def test_expire_secret_now_returns_404_for_unknown_token(hooks_client: TestClient) -> None:
    """A misnamed token on the e2e side would otherwise look like a
    silent no-op; the 404 fires loudly so the spec author knows the
    token didn't match. Pins the rowcount==0 branch in the handler."""
    r = hooks_client.post(
        "/_test/secret/nonexistent-token/expire-now",
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 404


def test_test_hooks_origin_gate_blocks_calls_from_other_origins(hooks_client: TestClient) -> None:
    """Both /_test/* endpoints carry Depends(verify_same_origin) so the
    `test_state_mutating_routes_all_carry_origin_gate` fitness invariant
    stays exception-free. Pins that the gate is actually applied at
    runtime, not just declared in the source."""
    r = hooks_client.post(
        "/_test/limiter/reset", headers={"Origin": "http://attacker.example"}
    )
    assert r.status_code == 403
