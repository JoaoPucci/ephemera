"""Tests for security headers, rate limiting, origin validation."""
import pytest


SEC_HEADERS = {
    "content-security-policy",
    "x-content-type-options",
    "x-frame-options",
    "referrer-policy",
    "strict-transport-security",
    "cross-origin-opener-policy",
    "cross-origin-resource-policy",
    "permissions-policy",
}


def test_security_headers_present_on_html_response(client):
    r = client.get("/send")
    for h in SEC_HEADERS:
        assert h in {k.lower() for k in r.headers}, f"missing header: {h}"


# ---------------------------------------------------------------------------
# Auth-gated API docs (/docs + /openapi.json)
#
# Unauthenticated callers must not be able to pull the wire contract (route
# list, parameter names, schemas). Authenticated operators -- either via a
# session cookie (web) or a bearer token (CLI) -- get the full Swagger UI.
# Assets are served from app/static/swagger/ rather than a CDN so the page
# works under our strict script-src 'self'.
# ---------------------------------------------------------------------------


def test_openapi_json_requires_auth(client):
    r = client.get("/openapi.json")
    assert r.status_code == 401


def test_openapi_json_accessible_with_bearer(client, auth_headers):
    r = client.get("/openapi.json", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert "openapi" in body
    assert "paths" in body
    # Sanity-check a few routes we'd expect to see in the schema.
    assert "/api/secrets" in body["paths"]
    assert "/send/login" in body["paths"]


def test_openapi_json_accessible_with_session(authed_client):
    r = authed_client.get("/openapi.json")
    assert r.status_code == 200
    assert "openapi" in r.json()


def test_docs_requires_auth(client):
    r = client.get("/docs")
    assert r.status_code == 401


def test_docs_accessible_with_session(authed_client):
    r = authed_client.get("/docs")
    assert r.status_code == 200
    html = r.text
    # Swagger UI assets are served locally, not from a CDN, so the CSP's
    # script-src 'self' doesn't need to be relaxed.
    assert '/static/swagger/swagger-ui-bundle.js' in html
    assert '/static/swagger/swagger-ui.css' in html
    assert '/static/swagger/init.js' in html


def test_swagger_static_assets_are_public_by_design(client):
    """`/docs` (the HTML shell) and `/openapi.json` (the schema) are auth-
    gated -- they're the real API surface that must not leak to unauthed
    probes. The Swagger UI vendor assets under `/static/swagger/` (JS
    bundle, CSS, favicon) are NOT gated, and this test pins that decision.

    The bundle is generic, pinned-version vendor code; hiding it from
    unauthed probes would reveal nothing about ephemera's routes or
    schemas (those stay behind the gate) while costing extra per-asset
    auth checks on every authenticated `/docs` visit. The only signal the
    public bundle leaks is "a Swagger UI is installed here," which is
    already implied by the `/docs` + `/openapi.json` 401 responses above.

    If a future change decides to gate these too, update this test
    deliberately rather than letting the 401 silently tell us the bundle
    stopped loading for real users."""
    for asset in (
        "swagger-ui-bundle.js",
        "swagger-ui.css",
        "favicon-32x32.png",
        "init.js",
    ):
        r = client.get(f"/static/swagger/{asset}")
        assert r.status_code == 200, f"/static/swagger/{asset} -> {r.status_code}"


def test_docs_html_contains_no_inline_scripts(authed_client):
    """The CSP is strict (script-src 'self'). The HTML shell must only
    reference external script files; any inline <script>...</script> block
    with a non-empty body would violate the policy and silently break
    Swagger UI in the browser."""
    import re

    r = authed_client.get("/docs")
    html = r.text
    # Find every <script ...>...</script>; reject any with non-empty content.
    for m in re.finditer(r"<script\b[^>]*>(.*?)</script>", html, flags=re.DOTALL):
        body = m.group(1).strip()
        assert body == "", f"inline script body found in /docs HTML: {body!r}"


def test_docs_is_not_advertised_in_openapi_schema(client, auth_headers):
    """/docs and /openapi.json themselves shouldn't appear as API routes
    in the schema they serve. include_in_schema=False on both prevents
    the meta-surface from bloating the docs."""
    r = client.get("/openapi.json", headers=auth_headers)
    paths = r.json()["paths"]
    assert "/docs" not in paths
    assert "/openapi.json" not in paths


def test_redoc_stays_disabled(client, auth_headers):
    """Swagger UI is the chosen docs surface; /redoc has no route mounted.
    Check both unauthenticated (404) and authenticated (still 404) so a
    future accidental re-enable is visible."""
    assert client.get("/redoc").status_code == 404
    assert client.get("/redoc", headers=auth_headers).status_code == 404


def test_security_headers_present_on_api_response(client, auth_headers):
    r = client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 300},
        headers=auth_headers,
    )
    for h in SEC_HEADERS:
        assert h in {k.lower() for k in r.headers}


def test_every_response_carries_every_security_header(
    client, authed_client, auth_headers
):
    """The middleware sets SECURITY_HEADERS unconditionally on every
    response. Pin that across a cross-section of real route shapes so a
    future change that lets a route override one of these values (or
    forgets to run it through the middleware at all) fails here.

    Compares exact values, not just presence, so "header is there but
    weakened" also trips this.
    """
    from app import SECURITY_HEADERS

    # Pre-register a secret so cancel/status routes have a real sid to hit.
    r = client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 300, "track": True},
        headers=auth_headers,
    )
    sid = r.json()["id"]

    def assert_full_headers(resp, label):
        for k, expected in SECURITY_HEADERS.items():
            got = resp.headers.get(k)
            assert got == expected, (
                f"{label}: {k!r} was {got!r}, expected {expected!r}"
            )

    # Cross-section of routes: page GET, API GETs, API POST, DELETE,
    # error status, static asset, the auth-gated docs surface.
    assert_full_headers(client.get("/send"), "GET /send")
    assert_full_headers(client.get("/api/me", headers=auth_headers), "GET /api/me")
    assert_full_headers(
        client.post(
            "/api/secrets",
            json={"content": "y", "content_type": "text", "expires_in": 300},
            headers=auth_headers,
        ),
        "POST /api/secrets (201)",
    )
    assert_full_headers(
        client.get(f"/api/secrets/{sid}/status", headers=auth_headers),
        "GET /api/secrets/{sid}/status",
    )
    assert_full_headers(
        client.get("/s/nonexistent-token/meta"),
        "GET /s/<bogus>/meta (404)",
    )
    assert_full_headers(client.get("/static/style.css"), "GET /static/style.css")
    assert_full_headers(authed_client.get("/docs"), "GET /docs (session-authed)")
    assert_full_headers(
        client.get("/openapi.json", headers=auth_headers),
        "GET /openapi.json (bearer-authed)",
    )
    # 401 error path -- security headers must still attach on the rejection.
    assert_full_headers(client.get("/api/me"), "GET /api/me (401)")


def test_x_content_type_options_is_nosniff(client):
    r = client.get("/send")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"


def test_x_frame_options_is_deny(client):
    r = client.get("/send")
    assert r.headers.get("X-Frame-Options") == "DENY"


def test_hsts_has_a_max_age(client):
    r = client.get("/send")
    hsts = r.headers.get("Strict-Transport-Security", "")
    assert "max-age=" in hsts


def test_csp_contains_expected_directives(client):
    """Pin the CSP shape so a future refactor can't silently drop directives.
    If you intentionally change CSP, update this list alongside the policy."""
    r = client.get("/send")
    csp = r.headers.get("Content-Security-Policy", "")
    expected = [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "font-src 'self'",
        "manifest-src 'self'",
        "frame-ancestors 'none'",
        "form-action 'self'",
        "base-uri 'self'",
        "object-src 'none'",
    ]
    for directive in expected:
        assert directive in csp, f"missing CSP directive: {directive!r} in {csp!r}"


def test_cross_origin_isolation_headers_present(client):
    r = client.get("/send")
    assert r.headers.get("Cross-Origin-Opener-Policy") == "same-origin"
    assert r.headers.get("Cross-Origin-Resource-Policy") == "same-origin"


def test_permissions_policy_denies_sensitive_features(client):
    r = client.get("/send")
    pp = r.headers.get("Permissions-Policy", "")
    for feature in ("camera", "microphone", "geolocation", "payment", "usb"):
        assert f"{feature}=()" in pp, f"permissions-policy does not deny {feature}: {pp!r}"


def test_post_api_secrets_without_origin_and_with_session_is_rejected(authed_client):
    """Browser clients must send Origin on state-changing requests. A
    session-cookie-authenticated POST with no Origin header is the
    CSRF-gap shape we refuse."""
    r = authed_client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 300},
    )
    assert r.status_code == 403


def test_post_api_secrets_without_origin_but_with_bearer_is_accepted(client, api_token):
    """Bearer-token (CLI/curl) callers have no ambient credentials and thus
    no CSRF risk. Missing Origin stays allowed for them."""
    r = client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 300},
        headers={"Authorization": f"Bearer {api_token}"},
    )
    assert r.status_code == 201


def test_post_api_secrets_without_origin_and_with_garbage_bearer_is_rejected(client):
    """`Authorization: Bearer anything` used to bypass the Origin gate
    because verify_same_origin only checked the prefix. Now the token is
    validated against the DB before missing-Origin is accepted; a bogus
    bearer produces the same 403 as missing-everything, not a 401 from
    the downstream auth check.

    Why this matters: both shapes of CSRF-risky request (missing Origin
    + cookie, missing Origin + fake bearer) now hit the same 403 gate
    uniformly, so the Origin check is strict on every browser-reachable
    path regardless of what bogus Authorization header the page attaches."""
    r = client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 300},
        headers={"Authorization": "Bearer totally-not-a-real-token"},
    )
    assert r.status_code == 403


def test_post_api_secrets_without_origin_and_with_empty_bearer_is_rejected(client):
    """`Authorization: Bearer ` (with no token after the space) is
    obviously-bogus and must be treated like any other missing-auth
    browser case -- 403 at the origin gate, not 401 at the auth layer."""
    r = client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 300},
        headers={"Authorization": "Bearer "},
    )
    assert r.status_code == 403


def test_delete_without_origin_and_with_session_is_rejected(authed_client):
    """Same policy on the DELETE verb, where historical browser Origin
    coverage is less uniform than POST."""
    r = authed_client.delete("/api/secrets/some-id")
    assert r.status_code == 403


def test_reveal_rejects_cross_origin_post(client, auth_headers):
    # Create a secret first with a valid origin.
    r = client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 300},
        headers=auth_headers,
    )
    url = r.json()["url"]
    token, frag = url.split("#", 1)
    token = token.rsplit("/", 1)[-1]
    bad = client.post(
        f"/s/{token}/reveal",
        json={"key": frag},
        headers={"Origin": "https://attacker.example"},
    )
    assert bad.status_code == 403


def test_rate_limiter_recovers_after_window_expires(monkeypatch):
    """Once the window elapses, old hits get popped from the queue and the
    same key can take a fresh round of hits. Deterministic via patching
    time.monotonic -- no wall-clock sleeping in the test."""
    from fastapi import HTTPException
    from app import limiter

    fake_time = [100.0]
    monkeypatch.setattr(limiter.time, "monotonic", lambda: fake_time[0])

    rl = limiter.RateLimiter(max_hits=2, window_seconds=60)
    rl.check("k")
    rl.check("k")
    with pytest.raises(HTTPException):
        rl.check("k")

    # Advance past the window; the old hits should fall out on the next call.
    fake_time[0] = 200.0
    rl.check("k")  # must not raise


def test_limiter_evicts_empty_buckets_on_check(monkeypatch):
    """When a key's bucket ages fully empty and that key hits again, the
    stale entry must be replaced rather than accumulated -- so a rotating-
    IP workload that happens to revisit keys doesn't leave dead empty
    deques littering the dict."""
    from app import limiter

    fake_time = [100.0]
    monkeypatch.setattr(limiter.time, "monotonic", lambda: fake_time[0])

    rl = limiter.RateLimiter(max_hits=2, window_seconds=60)
    rl.check("k")
    assert "k" in rl._hits and len(rl._hits["k"]) == 1

    # Past the window, the next check on the same key should replace the
    # entry (via del + re-create), not leave a stale empty deque behind.
    fake_time[0] = 200.0
    rl.check("k")
    assert len(rl._hits["k"]) == 1
    # Invariant: no entry with an empty deque ever lingers in the dict
    # after check() completes.
    assert all(len(q) > 0 for q in rl._hits.values())


def test_limiter_sweep_evicts_keys_that_never_return(monkeypatch):
    """The "attacker rotates source IPs, each hits once, never comes
    back" case. In-check lazy GC can't help -- nothing triggers a
    re-read of a key that's never queried again. sweep() walks the
    dict and drops fully-aged-out entries."""
    from app import limiter

    fake_time = [1000.0]
    monkeypatch.setattr(limiter.time, "monotonic", lambda: fake_time[0])

    rl = limiter.RateLimiter(max_hits=5, window_seconds=60)
    for i in range(50):
        rl.check(f"ip-{i}")
    assert len(rl._hits) == 50

    # No one comes back. Advance past the window.
    fake_time[0] = 2000.0

    evicted = rl.sweep()
    assert evicted == 50
    assert len(rl._hits) == 0


def test_limiter_sweep_keeps_keys_still_in_window(monkeypatch):
    """sweep() must not drop entries whose deques still have hits inside
    the window -- those are live buckets, not litter."""
    from app import limiter

    fake_time = [1000.0]
    monkeypatch.setattr(limiter.time, "monotonic", lambda: fake_time[0])

    rl = limiter.RateLimiter(max_hits=5, window_seconds=60)
    rl.check("recent")           # at t=1000
    fake_time[0] = 2000.0
    rl.check("even-more-recent") # at t=2000

    # Sweep at t=2010: "recent" is 1010s old (past the 60s window),
    # "even-more-recent" is 10s old (still in window).
    fake_time[0] = 2010.0
    evicted = rl.sweep()
    assert evicted == 1
    assert "recent" not in rl._hits
    assert "even-more-recent" in rl._hits


def test_cleanup_run_once_calls_sweep_on_every_limiter(monkeypatch, tmp_db_path):
    """cleanup.run_once() must advance sweep() across all four limiter
    instances so the bounded-memory invariant holds uniformly.

    tmp_db_path is required so run_once()'s DB-touching steps
    (purge_expired / purge_tracked_metadata) hit a real schema; otherwise
    the env-default path resolves to a missing ./ephemera.db under CI."""
    from app import cleanup, limiter

    called = []
    for name in ("reveal_limiter", "login_limiter", "create_limiter", "read_limiter"):
        lim = getattr(limiter, name)
        lim.reset()
        original = lim.sweep
        def wrapped(orig=original, n=name):
            called.append(n)
            return orig()
        monkeypatch.setattr(lim, "sweep", wrapped)

    cleanup.run_once()
    assert set(called) == {
        "reveal_limiter", "login_limiter", "create_limiter", "read_limiter"
    }


def test_read_rate_limit_kicks_in_on_meta_spam(client, auth_headers):
    """`/s/{token}/meta` used to have no rate limiter; a bogus-token probe
    loop could hammer the app indefinitely. The generic read limiter
    catches that past 300 req/min."""
    from app.limiter import read_limiter

    read_limiter.reset()
    statuses = []
    for i in range(320):
        r = client.get(f"/s/bogus-{i}/meta")
        statuses.append(r.status_code)
    assert 429 in statuses


def test_api_me_covered_by_read_rate_limit(client, auth_headers):
    """Hitting /api/me past the 300/min budget must 429 -- the endpoint
    used to have no limiter at all."""
    from app.limiter import read_limiter

    read_limiter.reset()
    statuses = []
    for _ in range(310):
        statuses.append(client.get("/api/me", headers=auth_headers).status_code)
    assert 429 in statuses


def test_reveal_rate_limit_kicks_in(client, auth_headers):
    # Hammer the reveal endpoint with bogus tokens. After the limit, responses are 429.
    statuses = []
    for i in range(15):
        resp = client.post(
            f"/s/bogus-{i}/reveal",
            json={"key": "AAAA"},
            headers={"Origin": "http://testserver"},
        )
        statuses.append(resp.status_code)
    assert 429 in statuses


# ---------------------------------------------------------------------------
# ProxyHeaders → limiter bucketing
# ---------------------------------------------------------------------------


def test_rate_limit_uses_forwarded_for_when_proxied():
    """Guards against the "everyone looks like the proxy's IP"
    regression. In prod, uvicorn is started with `--proxy-headers
    --forwarded-allow-ips 127.0.0.1` (see `docs/deployment.md`), which
    installs ProxyHeadersMiddleware; that middleware rewrites
    request.client.host from the trusted X-Forwarded-For value before
    any dependency runs. Our limiter keys on request.client.host (via
    _client_ip), so:

    - Two requests with the same X-Forwarded-For share a bucket.
    - Two requests with different X-Forwarded-For values get separate
      buckets.

    If anyone ever removes the middleware, or switches the limiter to
    an aggregated key (e.g., drops client.host), this test fails with
    both requests sharing a bucket (since TestClient's TCP peer is
    fixed)."""
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    from app.limiter import RateLimiter, _client_ip

    limiter = RateLimiter(max_hits=1, window_seconds=60)
    app = FastAPI()

    @app.get("/probe")
    def probe(req: Request):
        limiter.check(_client_ip(req))
        return {"ok": True, "ip": _client_ip(req)}

    # trusted_hosts="127.0.0.1" matches --forwarded-allow-ips 127.0.0.1.
    # TestClient's internal ASGI scope presents the request as coming
    # from "testclient"; treat any upstream as trusted inside the test
    # so ProxyHeadersMiddleware will honour X-Forwarded-For.
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

    client = TestClient(app)

    r1 = client.get("/probe", headers={"X-Forwarded-For": "1.2.3.4"})
    assert r1.status_code == 200
    assert r1.json()["ip"] == "1.2.3.4"

    # Same source IP: bucket exhausted on second call.
    r2 = client.get("/probe", headers={"X-Forwarded-For": "1.2.3.4"})
    assert r2.status_code == 429

    # Different source IP: fresh budget.
    r3 = client.get("/probe", headers={"X-Forwarded-For": "5.6.7.8"})
    assert r3.status_code == 200
    assert r3.json()["ip"] == "5.6.7.8"


# ---------------------------------------------------------------------------
# Lint: no sensitive-leaking patterns inside app/routes/
# ---------------------------------------------------------------------------


def test_landing_passphrase_input_is_type_password():
    """Receiver-side shoulder-surf hygiene: the passphrase <input> on the
    reveal landing page must be masked by default. A show/hide toggle is
    allowed to flip the type at runtime, but the rendered source must
    never ship with type=text. Mirrors the same invariant the sender-side
    form already holds; catching both sides is the point."""
    import pathlib
    import re

    html = (
        pathlib.Path(__file__).resolve().parent.parent
        / "app" / "templates" / "landing.html"
    ).read_text()
    # Find the passphrase input specifically; there may be other inputs on
    # the page in the future.
    pp_match = re.search(r'<input[^>]*id="passphrase"[^>]*>', html)
    assert pp_match is not None, "no #passphrase input found on landing page"
    tag = pp_match.group(0)
    assert 'type="password"' in tag, (
        f"receiver passphrase input must ship as type=password; got: {tag}"
    )


def test_login_code_input_has_show_hide_toggle_wiring():
    """The login form's code input ships as type=text (TOTP is the default
    mode; masking a 30-second rotating code buys no security). When the
    user toggles into recovery-code mode, login.js flips the input to
    type=password and unhides the show/hide button -- same pattern as
    every other passphrase field in the app.

    This test pins the HTML shape the JS relies on: the input lives inside
    an .input-with-action wrapper so the toggle button can be positioned
    next to it, and the toggle button exists (hidden by default) so
    setMode() can show it without a conditional DOM insertion. The JS
    state-machine is tested separately in tests-js/login.test.js."""
    import pathlib
    import re

    html = (
        pathlib.Path(__file__).resolve().parent.parent
        / "app" / "templates" / "login.html"
    ).read_text()

    # The code input must live inside an .input-with-action wrapper so the
    # toggle button renders adjacent to it.
    wrapper_match = re.search(
        r'<div[^>]*class="[^"]*input-with-action[^"]*"[^>]*>'
        r'(?:(?!</div>).)*<input[^>]*id="code"[^>]*>'
        r'(?:(?!</div>).)*<button[^>]*id="toggle-code"[^>]*>[^<]*</button>'
        r'(?:(?!</div>).)*</div>',
        html,
        flags=re.DOTALL,
    )
    assert wrapper_match is not None, (
        "login.html must wrap #code input in an .input-with-action div "
        "containing a #toggle-code button; setMode() requires this shape"
    )

    # The toggle button must ship hidden so TOTP mode (the default) doesn't
    # show it; setMode(true) unhides it when entering recovery mode.
    toggle_match = re.search(r'<button[^>]*id="toggle-code"[^>]*>', html)
    assert toggle_match is not None
    assert 'hidden' in toggle_match.group(0), (
        "login.html #toggle-code button must ship `hidden` so it isn't "
        "rendered in TOTP mode"
    )


def test_routes_do_not_log_tracebacks_or_grab_raw_body():
    """Invariant: route handlers must not call logger.exception or
    traceback.format_exc (tracebacks with locals leak plaintext/
    passphrase/client_half/password/totp_code), and must not
    `await request.body()` (that raw bytes blob is exactly the
    sensitive material we're trying not to bind to a name that could
    end up in a traceback frame). Use Pydantic / request.json() /
    request.form() instead; validate with a schema and keep the bytes
    out of local scope.

    Runs here as a pytest so it fires in the same pipeline as the
    other regression gates — a drive-by `logger.exception` in a new
    route fails the suite instead of reaching production."""
    import pathlib

    banned = [
        "logger.exception",
        "traceback.format_exc",
        "request.body()",
        "await request.body",
    ]
    routes_dir = pathlib.Path(__file__).resolve().parent.parent / "app" / "routes"
    offenders: list[str] = []
    for py_file in sorted(routes_dir.rglob("*.py")):
        content = py_file.read_text()
        for needle in banned:
            if needle in content:
                rel = py_file.relative_to(routes_dir.parent.parent)
                offenders.append(f"{rel}: {needle!r}")
    assert not offenders, (
        "Banned logging/body-capture patterns in app/routes/:\n  "
        + "\n  ".join(offenders)
        + "\n(See tests/test_security.py for the rationale.)"
    )
