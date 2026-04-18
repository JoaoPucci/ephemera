"""Tests for security headers, rate limiting, origin validation."""
import pytest


SEC_HEADERS = {
    "content-security-policy",
    "x-content-type-options",
    "x-frame-options",
    "referrer-policy",
    "strict-transport-security",
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


def test_post_api_secrets_without_origin_and_with_session_is_rejected(authed_client):
    """F-03 regression: browser clients must send Origin on state-changing
    requests. A session-cookie-authenticated POST with no Origin header is
    the CSRF-gap shape we refuse."""
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
    """Same F-03 policy on the DELETE verb, where historical browser
    Origin coverage is less uniform than POST."""
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
