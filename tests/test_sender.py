"""Tests for sender routes: login, logout, secret creation, status endpoint, user scoping."""
import pytest


# ---------------------------------------------------------------------------
# /send page rendering
# ---------------------------------------------------------------------------


def test_send_get_without_session_shows_login_page(client, provisioned_user):
    r = client.get("/send")
    assert r.status_code == 200
    body = r.content.lower()
    assert b"username" in body and b"password" in body and b"code" in body


def test_send_get_with_session_returns_form(authed_client):
    r = authed_client.get("/send")
    assert r.status_code == 200
    assert b"create" in r.content.lower()


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def _login(client, username, password, code):
    return client.post(
        "/send/login",
        data={"username": username, "password": password, "code": code},
        headers={"Origin": "http://testserver"},
    )


def test_login_wrong_username_rejected(client, provisioned_user):
    code = provisioned_user["totp"].now()
    r = _login(client, "nobody", provisioned_user["password"], code)
    assert r.status_code == 401


def test_login_wrong_password_rejected(client, provisioned_user):
    code = provisioned_user["totp"].now()
    r = _login(client, provisioned_user["username"], "nope", code)
    assert r.status_code == 401


def test_login_wrong_totp_rejected(client, provisioned_user):
    r = _login(client, provisioned_user["username"], provisioned_user["password"], "000000")
    assert r.status_code == 401


def test_login_correct_credentials_sets_session(client, provisioned_user):
    code = provisioned_user["totp"].now()
    r = _login(client, provisioned_user["username"], provisioned_user["password"], code)
    assert r.status_code == 200
    assert r.json()["username"] == provisioned_user["username"]
    from app.config import get_settings
    assert get_settings().session_cookie_name in r.cookies


def test_login_sets_secure_flag_on_session_cookie(
    tmp_db_path, provisioned_user, monkeypatch
):
    """The Secure attribute is critical so the session cookie never flows over
    plain HTTP. Pinned here so a future refactor can't silently flip it off.

    The default test env turns Secure off (TestClient uses http://, and a
    Secure cookie wouldn't round-trip). Flip it back on just for this test,
    rebuild a fresh client, and assert the flag is present in Set-Cookie.
    """
    from fastapi.testclient import TestClient
    from app import config, create_app
    from app.limiter import create_limiter, login_limiter, reveal_limiter

    monkeypatch.setenv("EPHEMERA_SESSION_COOKIE_SECURE", "true")
    config.get_settings.cache_clear()
    for lim in (reveal_limiter, login_limiter, create_limiter):
        lim.reset()

    with TestClient(create_app()) as c:
        code = provisioned_user["totp"].now()
        r = _login(c, provisioned_user["username"], provisioned_user["password"], code)

    set_cookie = r.headers.get("set-cookie", "").lower()
    assert "secure" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie


def test_login_same_error_for_wrong_user_vs_password_vs_totp(client, provisioned_user):
    """Enumeration resistance: every failure mode looks the same."""
    code = provisioned_user["totp"].now()
    r1 = _login(client, "nobody", provisioned_user["password"], code)
    r2 = _login(client, provisioned_user["username"], "wrong", code)
    r3 = _login(client, provisioned_user["username"], provisioned_user["password"], "000000")
    assert r1.status_code == r2.status_code == r3.status_code == 401
    assert r1.json() == r2.json() == r3.json()


def test_login_rotates_session_value_on_relogin(client, provisioned_user):
    import time
    r1 = _login(client, provisioned_user["username"], provisioned_user["password"], provisioned_user["totp"].now())
    from app.config import get_settings
    name = get_settings().session_cookie_name
    c1 = r1.cookies.get(name)
    time.sleep(1)
    future = provisioned_user["totp"].at(int(time.time()) + 30)
    r2 = _login(client, provisioned_user["username"], provisioned_user["password"], future)
    c2 = r2.cookies.get(name)
    assert c1 and c2 and c1 != c2


def test_session_invalidated_after_session_generation_bump(authed_client, provisioned_user):
    """Bumping the user's session_generation invalidates every live cookie
    signed over the prior generation. The existing session stops working
    without waiting for session_max_age."""
    from app import models

    assert authed_client.get("/api/me").status_code == 200
    models.bump_session_generation(provisioned_user["id"])
    assert authed_client.get("/api/me").status_code == 401


def test_new_login_after_bump_works(client, provisioned_user):
    """Bumping invalidates old cookies but not the ability to log in again;
    the next login picks up the new generation and the cookie authenticates."""
    from app import models

    models.bump_session_generation(provisioned_user["id"])
    r = _login(
        client,
        provisioned_user["username"],
        provisioned_user["password"],
        provisioned_user["totp"].now(),
    )
    assert r.status_code == 200
    assert client.get("/api/me").status_code == 200


def test_session_cookie_from_stale_generation_is_rejected(client, provisioned_user):
    """Defence-in-depth: an attacker who captured a cookie from generation N
    gains nothing once the user rotates to N+1, even before the timestamp
    expires. This is the property the generation counter buys us."""
    from app import models
    from app.dependencies import make_session_cookie
    from app.config import get_settings

    stale = make_session_cookie(provisioned_user["id"], session_generation=0)
    models.bump_session_generation(provisioned_user["id"])  # -> generation 1
    client.cookies.set(get_settings().session_cookie_name, stale)
    assert client.get("/api/me").status_code == 401


def test_login_rejects_cross_origin(client, provisioned_user):
    r = client.post(
        "/send/login",
        data={
            "username": provisioned_user["username"],
            "password": provisioned_user["password"],
            "code": provisioned_user["totp"].now(),
        },
        headers={"Origin": "https://attacker.example"},
    )
    assert r.status_code == 403


def test_login_rejects_oversized_form_fields(client, provisioned_user):
    """Caddy caps the full body at ~11MB; the app independently rejects
    fields that exceed sane bounds so we never spend bcrypt on obvious junk."""
    r = client.post(
        "/send/login",
        data={
            "username": "a" * 300,  # >256
            "password": provisioned_user["password"],
            "code": provisioned_user["totp"].now(),
        },
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 400

    r = client.post(
        "/send/login",
        data={
            "username": provisioned_user["username"],
            "password": "a" * 300,
            "code": provisioned_user["totp"].now(),
        },
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 400

    r = client.post(
        "/send/login",
        data={
            "username": provisioned_user["username"],
            "password": provisioned_user["password"],
            "code": "x" * 80,
        },
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 400


def test_login_rate_limit_kicks_in(client):
    statuses = [
        _login(client, "x", "x", "000000").status_code for _ in range(12)
    ]
    assert 429 in statuses


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


def test_api_me_returns_current_user(authed_client, provisioned_user):
    r = authed_client.get("/api/me")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == provisioned_user["id"]
    assert body["username"] == provisioned_user["username"]
    assert "email" in body  # may be None, but the key exists


def test_api_me_requires_auth(client):
    assert client.get("/api/me").status_code == 401


def test_api_me_works_with_api_token(client, auth_headers, provisioned_user):
    r = client.get("/api/me", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["username"] == provisioned_user["username"]


def test_logout_clears_session(authed_client):
    from app.config import get_settings
    r = authed_client.post("/send/logout", headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    assert get_settings().session_cookie_name in r.headers.get("set-cookie", "")


# ---------------------------------------------------------------------------
# Secret creation (bearer token)
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


def test_post_api_secrets_revoked_token_rejected(client, provisioned_user, api_token):
    from app import models
    models.revoke_token(provisioned_user["id"], "test")
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


def test_post_multipart_without_file_returns_422(client, auth_headers):
    """Multipart body with expires_in but no 'file' field (decoy-named so
    the request is still multipart/form-data) -- the handler refuses
    cleanly instead of crashing on a None lookup."""
    r = client.post(
        "/api/secrets",
        files={"decoy": ("decoy.bin", b"not-the-file-field", "application/octet-stream")},
        data={"expires_in": "3600"},
        headers={k: v for k, v in auth_headers.items() if k != "Content-Type"},
    )
    assert r.status_code == 422


def test_post_multipart_with_non_integer_expires_in_returns_422(
    client, auth_headers, sample_png_bytes
):
    """expires_in comes in as a form string; a non-numeric value is
    caught by the int() conversion and rejected."""
    r = client.post(
        "/api/secrets",
        files={"file": ("pic.png", sample_png_bytes, "image/png")},
        data={"expires_in": "not-a-number"},
        headers={k: v for k, v in auth_headers.items() if k != "Content-Type"},
    )
    assert r.status_code == 422


def test_post_multipart_with_non_preset_expires_in_returns_422(
    client, auth_headers, sample_png_bytes
):
    """Any integer that isn't in EXPIRY_PRESETS is rejected -- stops
    callers from passing arbitrary TTLs through the multipart path."""
    r = client.post(
        "/api/secrets",
        files={"file": ("pic.png", sample_png_bytes, "image/png")},
        data={"expires_in": "9999"},  # not in the preset set
        headers={k: v for k, v in auth_headers.items() if k != "Content-Type"},
    )
    assert r.status_code == 422


def test_post_multipart_rejects_oversized_passphrase(
    client, auth_headers, sample_png_bytes
):
    """JSON path already caps passphrase via the Pydantic model; the
    multipart path needs its own guard because it reads form fields raw."""
    r = client.post(
        "/api/secrets",
        files={"file": ("pic.png", sample_png_bytes, "image/png")},
        data={"expires_in": "3600", "passphrase": "x" * 250},
        headers={k: v for k, v in auth_headers.items() if k != "Content-Type"},
    )
    assert r.status_code == 422


def test_post_multipart_rejects_oversized_label(
    client, auth_headers, sample_png_bytes
):
    r = client.post(
        "/api/secrets",
        files={"file": ("pic.png", sample_png_bytes, "image/png")},
        data={"expires_in": "3600", "label": "x" * 100},
        headers={k: v for k, v in auth_headers.items() if k != "Content-Type"},
    )
    assert r.status_code == 422


def test_post_api_secrets_unsupported_content_type_returns_415(client, auth_headers):
    """Anything that isn't application/json or multipart/form-data is
    refused before hitting the crypto layer."""
    r = client.post(
        "/api/secrets",
        content=b"raw bytes",
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert r.status_code == 415


def test_post_api_secrets_with_passphrase_stored_as_bcrypt_hash(client, auth_headers):
    r = client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 3600, "passphrase": "horse"},
        headers=auth_headers,
    )
    assert r.status_code == 201
    from app import models
    row = models.get_by_id(r.json()["id"], 1)
    assert row["passphrase"] is not None and row["passphrase"].startswith("$2")


def test_post_api_secrets_with_track_sets_flag(client, auth_headers):
    r = client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 3600, "track": True},
        headers=auth_headers,
    )
    from app import models
    assert models.get_by_id(r.json()["id"], 1)["track"] in (1, True)


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


def test_post_api_secrets_assigns_to_authenticated_user(client, auth_headers, provisioned_user):
    r = client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 300},
        headers=auth_headers,
    )
    from app import models
    row = models.get_by_id(r.json()["id"], 1)
    assert row["user_id"] == provisioned_user["id"]


# ---------------------------------------------------------------------------
# Session-auth path
# ---------------------------------------------------------------------------


def test_create_secret_via_session_without_bearer_works(authed_client):
    r = authed_client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 300},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 201


# ---------------------------------------------------------------------------
# Labels + tracked-secrets list + delete (user-scoped)
# ---------------------------------------------------------------------------


def test_label_stored_when_tracking_enabled(client, auth_headers):
    r = client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 300,
              "track": True, "label": "API key for Acme"},
        headers=auth_headers,
    )
    from app import models
    row = models.get_by_id(r.json()["id"], 1)
    assert row["label"] == "API key for Acme"


def test_label_ignored_without_tracking(client, auth_headers):
    r = client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 300,
              "track": False, "label": "should not stick"},
        headers=auth_headers,
    )
    from app import models
    assert models.get_by_id(r.json()["id"], 1)["label"] is None


def test_list_tracked_returns_only_current_users_secrets(client, auth_headers, provisioned_user, make_user):
    # Alice creates a tracked secret via her API token.
    client.post(
        "/api/secrets",
        json={"content": "alice", "content_type": "text", "expires_in": 300,
              "track": True, "label": "alice-one"},
        headers=auth_headers,
    )
    # Bob (different user, different token) creates his own.
    from app import auth, models
    bob = make_user("bob")
    plaintext, digest = auth.mint_api_token()
    models.create_token(user_id=bob["id"], name="bob-test", token_hash=digest)
    bob_hdrs = {"Authorization": f"Bearer {plaintext}", "Origin": "http://testserver"}
    client.post(
        "/api/secrets",
        json={"content": "bob", "content_type": "text", "expires_in": 300,
              "track": True, "label": "bob-one"},
        headers=bob_hdrs,
    )
    # Alice sees only her secret.
    ar = client.get("/api/secrets/tracked", headers=auth_headers).json()["items"]
    assert [i["label"] for i in ar] == ["alice-one"]
    # Bob sees only his.
    br = client.get("/api/secrets/tracked", headers=bob_hdrs).json()["items"]
    assert [i["label"] for i in br] == ["bob-one"]


def test_status_endpoint_returns_pending_for_tracked(client, auth_headers):
    r = client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 3600, "track": True},
        headers=auth_headers,
    )
    sid = r.json()["id"]
    s = client.get(f"/api/secrets/{sid}/status", headers=auth_headers)
    assert s.status_code == 200 and s.json()["status"] == "pending"


def test_status_endpoint_404_for_other_users_secret(client, auth_headers, make_user):
    r = client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 3600, "track": True},
        headers=auth_headers,
    )
    sid = r.json()["id"]

    from app import auth, models
    bob = make_user("bob")
    plaintext, digest = auth.mint_api_token()
    models.create_token(user_id=bob["id"], name="bob-test", token_hash=digest)
    bob_hdrs = {"Authorization": f"Bearer {plaintext}", "Origin": "http://testserver"}

    # Bob must not be able to read Alice's status.
    s = client.get(f"/api/secrets/{sid}/status", headers=bob_hdrs)
    assert s.status_code == 404


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
    assert client.get("/api/secrets/some-id/status").status_code == 401


def test_delete_untracks_pending_secret_but_keeps_url_live(client, auth_headers):
    r = client.post(
        "/api/secrets",
        json={"content": "t", "content_type": "text", "expires_in": 300, "track": True},
        headers=auth_headers,
    )
    sid = r.json()["id"]
    url = r.json()["url"]
    token, frag = url.split("#", 1)
    token = token.rsplit("/", 1)[-1]

    d = client.delete(f"/api/secrets/{sid}", headers=auth_headers)
    assert d.status_code == 204

    assert client.get("/api/secrets/tracked", headers=auth_headers).json()["items"] == []
    rv = client.post(f"/s/{token}/reveal", json={"key": frag}, headers={"Origin": "http://testserver"})
    assert rv.status_code == 200


def test_delete_cannot_touch_other_users_secret(client, auth_headers, make_user):
    r = client.post(
        "/api/secrets",
        json={"content": "t", "content_type": "text", "expires_in": 300, "track": True},
        headers=auth_headers,
    )
    sid = r.json()["id"]

    from app import auth, models
    bob = make_user("bob")
    plaintext, digest = auth.mint_api_token()
    models.create_token(user_id=bob["id"], name="bob-test", token_hash=digest)
    bob_hdrs = {"Authorization": f"Bearer {plaintext}", "Origin": "http://testserver"}

    # Bob's DELETE is idempotent 204 but must not affect Alice's secret.
    d = client.delete(f"/api/secrets/{sid}", headers=bob_hdrs)
    assert d.status_code == 204
    # Alice's tracked list still has her secret.
    items = client.get("/api/secrets/tracked", headers=auth_headers).json()["items"]
    assert len(items) == 1


def test_delete_is_idempotent(client, auth_headers):
    d = client.delete("/api/secrets/does-not-exist", headers=auth_headers)
    assert d.status_code == 204


def test_delete_requires_auth(client):
    assert (
        client.delete(
            "/api/secrets/some-id", headers={"Origin": "http://testserver"}
        ).status_code
        == 401
    )


# ---------------------------------------------------------------------------
# Cancel: sender revokes the URL before receiver opens it
# ---------------------------------------------------------------------------


def test_cancel_revokes_url_and_tags_as_canceled(client, auth_headers):
    r = client.post(
        "/api/secrets",
        json={"content": "regret", "content_type": "text", "expires_in": 300, "track": True},
        headers=auth_headers,
    )
    sid = r.json()["id"]
    url = r.json()["url"]
    token, frag = url.split("#", 1)
    token = token.rsplit("/", 1)[-1]

    c = client.post(f"/api/secrets/{sid}/cancel", headers=auth_headers)
    assert c.status_code == 204

    # Receiver URL now returns 404.
    rv = client.post(f"/s/{token}/reveal", json={"key": frag}, headers={"Origin": "http://testserver"})
    assert rv.status_code == 404

    # Tracked list shows it as canceled (row retained for audit).
    items = client.get("/api/secrets/tracked", headers=auth_headers).json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "canceled"
    assert items[0]["viewed_at"] is not None


def test_cancel_on_already_viewed_returns_404(client, auth_headers):
    r = client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 300, "track": True},
        headers=auth_headers,
    )
    sid = r.json()["id"]
    url = r.json()["url"]
    token, frag = url.split("#", 1)
    token = token.rsplit("/", 1)[-1]
    client.post(f"/s/{token}/reveal", json={"key": frag}, headers={"Origin": "http://testserver"})

    # Already viewed -> ciphertext gone -> cancel has nothing to do.
    c = client.post(f"/api/secrets/{sid}/cancel", headers=auth_headers)
    assert c.status_code == 404


def test_cancel_cannot_touch_other_users_secret(client, auth_headers, make_user):
    r = client.post(
        "/api/secrets",
        json={"content": "mine", "content_type": "text", "expires_in": 300, "track": True},
        headers=auth_headers,
    )
    sid = r.json()["id"]
    url = r.json()["url"]
    token, frag = url.split("#", 1)
    token = token.rsplit("/", 1)[-1]

    # Bob tries to cancel Alice's secret.
    from app import auth, models
    bob = make_user("bob")
    plaintext, digest = auth.mint_api_token()
    models.create_token(user_id=bob["id"], name="bob-test", token_hash=digest)
    bob_hdrs = {"Authorization": f"Bearer {plaintext}", "Origin": "http://testserver"}

    c = client.post(f"/api/secrets/{sid}/cancel", headers=bob_hdrs)
    assert c.status_code == 404

    # Alice's URL still works.
    rv = client.post(f"/s/{token}/reveal", json={"key": frag}, headers={"Origin": "http://testserver"})
    assert rv.status_code == 200


def test_cancel_requires_auth(client):
    r = client.post(
        "/api/secrets/whatever/cancel", headers={"Origin": "http://testserver"}
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Clear-history: batch-delete non-pending tracked rows
# ---------------------------------------------------------------------------


def test_clear_history_keeps_pending_and_deletes_the_rest(client, auth_headers):
    # One pending, one viewed, one canceled
    live = client.post(
        "/api/secrets",
        json={"content": "live", "content_type": "text", "expires_in": 300, "track": True},
        headers=auth_headers,
    ).json()

    viewed = client.post(
        "/api/secrets",
        json={"content": "viewed", "content_type": "text", "expires_in": 300, "track": True},
        headers=auth_headers,
    ).json()
    v_token, v_frag = viewed["url"].split("#", 1)
    v_token = v_token.rsplit("/", 1)[-1]
    client.post(f"/s/{v_token}/reveal", json={"key": v_frag}, headers={"Origin": "http://testserver"})

    canceled = client.post(
        "/api/secrets",
        json={"content": "canceled", "content_type": "text", "expires_in": 300, "track": True},
        headers=auth_headers,
    ).json()
    client.post(f"/api/secrets/{canceled['id']}/cancel", headers=auth_headers)

    r = client.post("/api/secrets/tracked/clear", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == {"cleared": 2}

    items = client.get("/api/secrets/tracked", headers=auth_headers).json()["items"]
    assert [i["id"] for i in items] == [live["id"]]


def test_clear_history_scopes_by_user(client, auth_headers, make_user):
    # Alice creates + views a secret (will become clearable).
    r = client.post(
        "/api/secrets",
        json={"content": "a", "content_type": "text", "expires_in": 300, "track": True},
        headers=auth_headers,
    ).json()
    t, f = r["url"].split("#", 1)
    t = t.rsplit("/", 1)[-1]
    client.post(f"/s/{t}/reveal", json={"key": f}, headers={"Origin": "http://testserver"})

    # Bob also has a viewed tracked secret.
    from app import auth, models
    bob = make_user("bob")
    plaintext, digest = auth.mint_api_token()
    models.create_token(user_id=bob["id"], name="bob-test", token_hash=digest)
    bob_hdrs = {"Authorization": f"Bearer {plaintext}", "Origin": "http://testserver"}
    br = client.post(
        "/api/secrets",
        json={"content": "b", "content_type": "text", "expires_in": 300, "track": True},
        headers=bob_hdrs,
    ).json()
    bt, bf = br["url"].split("#", 1)
    bt = bt.rsplit("/", 1)[-1]
    client.post(f"/s/{bt}/reveal", json={"key": bf}, headers={"Origin": "http://testserver"})

    # Alice clears her history; Bob's should survive.
    ar = client.post("/api/secrets/tracked/clear", headers=auth_headers)
    assert ar.status_code == 200 and ar.json()["cleared"] == 1
    assert client.get("/api/secrets/tracked", headers=auth_headers).json()["items"] == []
    bobs = client.get("/api/secrets/tracked", headers=bob_hdrs).json()["items"]
    assert len(bobs) == 1


def test_clear_history_returns_zero_when_nothing_to_clear(client, auth_headers):
    client.post(
        "/api/secrets",
        json={"content": "live", "content_type": "text", "expires_in": 300, "track": True},
        headers=auth_headers,
    )
    r = client.post("/api/secrets/tracked/clear", headers=auth_headers)
    assert r.status_code == 200 and r.json()["cleared"] == 0


def test_clear_history_requires_auth(client):
    assert (
        client.post(
            "/api/secrets/tracked/clear", headers={"Origin": "http://testserver"}
        ).status_code
        == 401
    )


def test_clear_history_rejects_cross_origin(client, auth_headers):
    bad = {"Authorization": auth_headers["Authorization"], "Origin": "https://attacker.example"}
    assert client.post("/api/secrets/tracked/clear", headers=bad).status_code == 403


def test_cancel_rejects_cross_origin(client, auth_headers):
    r = client.post(
        "/api/secrets",
        json={"content": "x", "content_type": "text", "expires_in": 300, "track": True},
        headers=auth_headers,
    )
    sid = r.json()["id"]
    bad = {"Authorization": auth_headers["Authorization"], "Origin": "https://attacker.example"}
    c = client.post(f"/api/secrets/{sid}/cancel", headers=bad)
    assert c.status_code == 403
