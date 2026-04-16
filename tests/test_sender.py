"""Tests for sender routes: auth, form rendering, secret creation, status endpoint."""
import io

import pytest


def test_send_get_without_session_shows_login_page(client):
    r = client.get("/send")
    assert r.status_code == 200
    assert b"API key" in r.content or b"api_key" in r.content.lower()


def test_send_login_wrong_api_key_rejected(client):
    r = client.post("/send/login", data={"api_key": "wrong"}, follow_redirects=False)
    assert r.status_code in (401, 403)


def test_send_login_correct_api_key_sets_session_cookie(client, api_key):
    r = client.post(
        "/send/login",
        data={"api_key": api_key},
        follow_redirects=False,
    )
    assert r.status_code in (200, 302, 303)
    from app.config import get_settings
    assert get_settings().session_cookie_name in r.cookies


def test_send_get_with_session_returns_form(client, api_key):
    client.post("/send/login", data={"api_key": api_key})
    r = client.get("/send")
    assert r.status_code == 200
    assert b"Create Secret" in r.content or b"create" in r.content.lower()


def test_post_api_secrets_without_bearer_token_rejected(client):
    r = client.post("/api/secrets", json={"content": "x", "content_type": "text", "expires_in": 300})
    assert r.status_code == 401


def test_post_api_secrets_wrong_bearer_token_rejected(client):
    r = client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 300},
        headers={"Authorization": "Bearer wrong", "Origin": "http://testserver"},
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
    assert "/s/" in url
    assert "#" in url
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
    assert r.status_code == 413 or r.status_code == 400


def test_post_api_secrets_with_passphrase_stored_as_bcrypt_hash(client, auth_headers):
    r = client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 3600, "passphrase": "correct horse"},
        headers=auth_headers,
    )
    assert r.status_code == 201
    from app import models
    sid = r.json()["id"]
    row = models.get_by_id(sid)
    assert row["passphrase"] is not None
    assert row["passphrase"].startswith("$2")  # bcrypt prefix


def test_post_api_secrets_with_track_sets_flag(client, auth_headers):
    r = client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 3600, "track": True},
        headers=auth_headers,
    )
    sid = r.json()["id"]
    from app import models
    assert models.get_by_id(sid)["track"] in (1, True)


def test_post_api_secrets_invalid_expiry_rejected(client, auth_headers):
    r = client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 99},  # not a preset
        headers=auth_headers,
    )
    assert r.status_code == 422 or r.status_code == 400


def test_post_api_secrets_all_expiry_presets_accepted(client, auth_headers):
    for expires_in in [300, 1800, 3600, 14400, 43200, 86400, 259200, 604800]:
        r = client.post(
            "/api/secrets",
            json={"content": "x", "content_type": "text", "expires_in": expires_in},
            headers=auth_headers,
        )
        assert r.status_code == 201, f"expiry {expires_in} rejected"


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
