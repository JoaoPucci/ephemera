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


def test_security_headers_present_on_api_response(client, auth_headers):
    r = client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 300},
        headers=auth_headers,
    )
    for h in SEC_HEADERS:
        assert h in {k.lower() for k in r.headers}


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
        / "app" / "static" / "landing.html"
    ).read_text()
    # Find the passphrase input specifically; there may be other inputs on
    # the page in the future.
    pp_match = re.search(r'<input[^>]*id="passphrase"[^>]*>', html)
    assert pp_match is not None, "no #passphrase input found on landing page"
    tag = pp_match.group(0)
    assert 'type="password"' in tag, (
        f"receiver passphrase input must ship as type=password; got: {tag}"
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

    Raising this guard to a CI-level lint was suggested by the audit's
    §10 Q#12 follow-up. Implemented here as a pytest so it runs in
    the same pipeline as the other regression gates."""
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
