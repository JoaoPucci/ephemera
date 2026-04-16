"""Tests for sender routes: login, logout, secret creation, status endpoint."""
import pytest


# ---------------------------------------------------------------------------
# /send page rendering
# ---------------------------------------------------------------------------


def test_send_get_without_session_shows_login_page(client, provisioned_user):
    r = client.get("/send")
    assert r.status_code == 200
    body = r.content.lower()
    assert b"password" in body and b"code" in body


def test_send_get_with_session_returns_form(authed_client):
    r = authed_client.get("/send")
    assert r.status_code == 200
    assert b"create" in r.content.lower()


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def test_login_wrong_password_rejected(client, provisioned_user):
    code = provisioned_user["totp"].now()
    r = client.post(
        "/send/login",
        data={"password": "nope", "code": code},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code == 401


def test_login_wrong_totp_rejected(client, provisioned_user):
    r = client.post(
        "/send/login",
        data={"password": provisioned_user["password"], "code": "000000"},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 401


def test_login_missing_totp_rejected(client, provisioned_user):
    r = client.post(
        "/send/login",
        data={"password": provisioned_user["password"], "code": ""},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 401 or r.status_code == 422


def test_login_correct_password_and_totp_sets_session(client, provisioned_user):
    code = provisioned_user["totp"].now()
    r = client.post(
        "/send/login",
        data={"password": provisioned_user["password"], "code": code},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200
    from app.config import get_settings
    assert get_settings().session_cookie_name in r.cookies


def test_login_same_error_code_for_wrong_password_and_wrong_totp(client, provisioned_user):
    """Enumeration resistance: caller can't tell which factor failed."""
    r1 = client.post(
        "/send/login",
        data={"password": "wrong", "code": provisioned_user["totp"].now()},
        headers={"Origin": "http://testserver"},
    )
    r2 = client.post(
        "/send/login",
        data={"password": provisioned_user["password"], "code": "000000"},
        headers={"Origin": "http://testserver"},
    )
    assert r1.status_code == r2.status_code == 401
    assert r1.json() == r2.json()


def test_login_rotates_session_value_on_relogin(client, provisioned_user):
    code1 = provisioned_user["totp"].now()
    r1 = client.post(
        "/send/login",
        data={"password": provisioned_user["password"], "code": code1},
        headers={"Origin": "http://testserver"},
    )
    from app.config import get_settings
    cookie_name = get_settings().session_cookie_name
    c1 = r1.cookies.get(cookie_name)
    # wait then re-login with a fresh code
    import time
    time.sleep(1)
    code2 = provisioned_user["totp"].at(int(time.time()) + 30)
    r2 = client.post(
        "/send/login",
        data={"password": provisioned_user["password"], "code": code2},
        headers={"Origin": "http://testserver"},
    )
    c2 = r2.cookies.get(cookie_name)
    assert c1 and c2 and c1 != c2


def test_login_rejects_cross_origin(client, provisioned_user):
    code = provisioned_user["totp"].now()
    r = client.post(
        "/send/login",
        data={"password": provisioned_user["password"], "code": code},
        headers={"Origin": "https://attacker.example"},
    )
    assert r.status_code == 403


def test_login_rate_limit_kicks_in(client):
    statuses = []
    for _ in range(12):
        r = client.post(
            "/send/login",
            data={"password": "x", "code": "000000"},
            headers={"Origin": "http://testserver"},
        )
        statuses.append(r.status_code)
    assert 429 in statuses


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


def test_logout_clears_session(authed_client):
    from app.config import get_settings
    r = authed_client.post("/send/logout", headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    # cookie should be cleared (set-cookie with Max-Age=0)
    set_cookie = r.headers.get("set-cookie", "")
    assert get_settings().session_cookie_name in set_cookie


# ---------------------------------------------------------------------------
# Secret creation (bearer token path)
# ---------------------------------------------------------------------------


def test_post_api_secrets_without_bearer_token_rejected(client):
    r = client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 300},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 401


def test_post_api_secrets_wrong_bearer_token_rejected(client):
    r = client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 300},
        headers={"Authorization": "Bearer wrong", "Origin": "http://testserver"},
    )
    assert r.status_code == 401


def test_post_api_secrets_revoked_token_rejected(client, api_token):
    from app import models
    models.revoke_token("test")
    r = client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 300},
        headers={"Authorization": f"Bearer {api_token}", "Origin": "http://testserver"},
    )
    assert r.status_code == 401


def test_post_api_secrets_text_creates_secret(client, auth_headers):
    r = client.post(
        "/api/secrets",
        json={"content": "hello world", "content_type": "text", "expires_in": 3600},
        headers=auth_headers,
    )
    assert r.status_code == 201
    body = r.json()
    assert "url" in body and "id" in body and "expires_at" in body


def test_post_api_secrets_text_returns_url_with_fragment(client, auth_headers):
    r = client.post(
        "/api/secrets",
        json={"content": "hello", "content_type": "text", "expires_in": 3600},
        headers=auth_headers,
    )
    url = r.json()["url"]
    assert "/s/" in url and "#" in url
    _, frag = url.split("#", 1)
    assert len(frag) >= 16


def test_post_api_secrets_image_multipart_creates_secret(client, auth_headers, sample_png_bytes):
    r = client.post(
        "/api/secrets",
        files={"file": ("pic.png", sample_png_bytes, "image/png")},
        data={"expires_in": "3600"},
        headers={k: v for k, v in auth_headers.items() if k != "Content-Type"},
    )
    assert r.status_code == 201, r.text
    assert "url" in r.json()


def test_post_api_secrets_rejects_svg_upload(client, auth_headers, sample_svg_bytes):
    r = client.post(
        "/api/secrets",
        files={"file": ("evil.svg", sample_svg_bytes, "image/svg+xml")},
        data={"expires_in": "3600"},
        headers={k: v for k, v in auth_headers.items() if k != "Content-Type"},
    )
    assert r.status_code == 400


def test_post_api_secrets_rejects_oversize_image(client, auth_headers):
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (11 * 1024 * 1024)
    r = client.post(
        "/api/secrets",
        files={"file": ("big.png", big, "image/png")},
        data={"expires_in": "3600"},
        headers={k: v for k, v in auth_headers.items() if k != "Content-Type"},
    )
    assert r.status_code in (400, 413)


def test_post_api_secrets_with_passphrase_stored_as_bcrypt_hash(client, auth_headers):
    r = client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 3600, "passphrase": "horse"},
        headers=auth_headers,
    )
    assert r.status_code == 201
    from app import models
    row = models.get_by_id(r.json()["id"])
    assert row["passphrase"] is not None
    assert row["passphrase"].startswith("$2")


def test_post_api_secrets_with_track_sets_flag(client, auth_headers):
    r = client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 3600, "track": True},
        headers=auth_headers,
    )
    from app import models
    assert models.get_by_id(r.json()["id"])["track"] in (1, True)


def test_post_api_secrets_invalid_expiry_rejected(client, auth_headers):
    r = client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 99},
        headers=auth_headers,
    )
    assert r.status_code in (400, 422)


def test_post_api_secrets_all_expiry_presets_accepted(client, auth_headers):
    for expires_in in [300, 1800, 3600, 14400, 43200, 86400, 259200, 604800]:
        r = client.post(
            "/api/secrets",
            json={"content": "x", "content_type": "text", "expires_in": expires_in},
            headers=auth_headers,
        )
        assert r.status_code == 201, f"expiry {expires_in} rejected"


# ---------------------------------------------------------------------------
# Session-auth path (web form)
# ---------------------------------------------------------------------------


def test_create_secret_via_session_without_bearer_works(authed_client):
    r = authed_client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 300},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 201


# ---------------------------------------------------------------------------
# Status endpoint
# ---------------------------------------------------------------------------


def test_status_endpoint_returns_pending_for_tracked(client, auth_headers):
    r = client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 3600, "track": True},
        headers=auth_headers,
    )
    sid = r.json()["id"]
    s = client.get(f"/api/secrets/{sid}/status", headers=auth_headers)
    assert s.status_code == 200
    assert s.json()["status"] == "pending"


def test_status_endpoint_404_for_untracked(client, auth_headers):
    r = client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 3600, "track": False},
        headers=auth_headers,
    )
    sid = r.json()["id"]
    s = client.get(f"/api/secrets/{sid}/status", headers=auth_headers)
    assert s.status_code == 404


def test_status_endpoint_requires_auth(client):
    s = client.get("/api/secrets/some-id/status")
    assert s.status_code == 401
