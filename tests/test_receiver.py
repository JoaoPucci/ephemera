"""Tests for receiver routes: landing page, reveal, passphrase, burn-on-failure."""
import pytest


def _create_text_secret(client, auth_headers, content="the secret", passphrase=None, track=False):
    body = {"content": content, "content_type": "text", "expires_in": 3600, "track": track}
    if passphrase is not None:
        body["passphrase"] = passphrase
    r = client.post("/api/secrets", json=body, headers=auth_headers)
    assert r.status_code == 201, r.text
    return r.json()


def _token_and_client_half(url):
    path, frag = url.split("#", 1)
    token = path.rsplit("/", 1)[-1]
    return token, frag


def test_landing_page_returned_for_any_token(client):
    # Even for bogus tokens, landing HTML is returned; meta endpoint reveals state.
    r = client.get("/s/totally-fake-token")
    assert r.status_code == 200
    assert b"<html" in r.content.lower() or b"<!doctype" in r.content.lower()


def test_meta_returns_passphrase_false_when_none(client, auth_headers):
    secret = _create_text_secret(client, auth_headers)
    token, _ = _token_and_client_half(secret["url"])
    r = client.get(f"/s/{token}/meta")
    assert r.status_code == 200
    assert r.json()["passphrase_required"] is False


def test_meta_returns_passphrase_true_when_set(client, auth_headers):
    secret = _create_text_secret(client, auth_headers, passphrase="open sesame")
    token, _ = _token_and_client_half(secret["url"])
    r = client.get(f"/s/{token}/meta")
    assert r.status_code == 200
    assert r.json()["passphrase_required"] is True


def test_meta_returns_404_for_unknown_token(client):
    r = client.get("/s/nonexistent/meta")
    assert r.status_code == 404


def test_reveal_returns_plaintext_for_text_secret(client, auth_headers):
    secret = _create_text_secret(client, auth_headers, content="hello")
    token, client_half = _token_and_client_half(secret["url"])
    r = client.post(
        f"/s/{token}/reveal",
        json={"key": client_half},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["content_type"] == "text"
    assert body["content"] == "hello"


def test_reveal_deletes_secret_on_success(client, auth_headers):
    secret = _create_text_secret(client, auth_headers)
    token, client_half = _token_and_client_half(secret["url"])
    client.post(f"/s/{token}/reveal", json={"key": client_half}, headers={"Origin": "http://testserver"})
    from app import models
    assert models.get_by_token(token) is None


def test_reveal_twice_second_returns_404(client, auth_headers):
    secret = _create_text_secret(client, auth_headers)
    token, client_half = _token_and_client_half(secret["url"])
    client.post(f"/s/{token}/reveal", json={"key": client_half}, headers={"Origin": "http://testserver"})
    r = client.post(f"/s/{token}/reveal", json={"key": client_half}, headers={"Origin": "http://testserver"})
    assert r.status_code == 404


def test_reveal_with_wrong_key_returns_error(client, auth_headers):
    secret = _create_text_secret(client, auth_headers)
    token, _ = _token_and_client_half(secret["url"])
    import base64
    bad = base64.urlsafe_b64encode(b"\x00" * 16).rstrip(b"=").decode()
    r = client.post(f"/s/{token}/reveal", json={"key": bad}, headers={"Origin": "http://testserver"})
    assert r.status_code == 400


def test_reveal_without_passphrase_when_required_rejected(client, auth_headers):
    secret = _create_text_secret(client, auth_headers, passphrase="pw")
    token, client_half = _token_and_client_half(secret["url"])
    r = client.post(f"/s/{token}/reveal", json={"key": client_half}, headers={"Origin": "http://testserver"})
    assert r.status_code == 401


def test_reveal_with_wrong_passphrase_returns_401_and_increments(client, auth_headers):
    secret = _create_text_secret(client, auth_headers, passphrase="correct")
    token, client_half = _token_and_client_half(secret["url"])
    r = client.post(
        f"/s/{token}/reveal",
        json={"key": client_half, "passphrase": "wrong"},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 401
    from app import models
    row = models.get_by_token(token)
    assert row is not None
    assert row["attempts"] == 1


def test_reveal_with_correct_passphrase_succeeds(client, auth_headers):
    secret = _create_text_secret(client, auth_headers, content="payload", passphrase="correct")
    token, client_half = _token_and_client_half(secret["url"])
    r = client.post(
        f"/s/{token}/reveal",
        json={"key": client_half, "passphrase": "correct"},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200
    assert r.json()["content"] == "payload"


def test_reveal_burns_secret_after_too_many_failed_passphrase_attempts(client, auth_headers):
    secret = _create_text_secret(client, auth_headers, passphrase="correct")
    token, client_half = _token_and_client_half(secret["url"])
    last_status = None
    for _ in range(5):
        last = client.post(
            f"/s/{token}/reveal",
            json={"key": client_half, "passphrase": "wrong"},
            headers={"Origin": "http://testserver"},
        )
        last_status = last.status_code
    # After 5 failed attempts the secret is burned.
    assert last_status in (401, 410)
    r = client.post(
        f"/s/{token}/reveal",
        json={"key": client_half, "passphrase": "correct"},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code in (404, 410)


def test_reveal_returns_image_as_base64(client, auth_headers, sample_png_bytes):
    r = client.post(
        "/api/secrets",
        files={"file": ("a.png", sample_png_bytes, "image/png")},
        data={"expires_in": "3600"},
        headers={k: v for k, v in auth_headers.items() if k != "Content-Type"},
    )
    assert r.status_code == 201, r.text
    url = r.json()["url"]
    token, client_half = _token_and_client_half(url)
    rv = client.post(f"/s/{token}/reveal", json={"key": client_half}, headers={"Origin": "http://testserver"})
    assert rv.status_code == 200
    body = rv.json()
    assert body["content_type"] == "image"
    assert body["mime_type"] == "image/png"
    import base64
    assert base64.b64decode(body["content"]) == sample_png_bytes


def test_reveal_404_for_expired_secret(client, auth_headers):
    from app import models, crypto
    # Create directly with negative expiry.
    key = crypto.generate_key()
    server_half, client_half = crypto.split_key(key)
    ct = crypto.encrypt(b"x", key)
    r = models.create_secret(
        content_type="text", mime_type=None, ciphertext=ct,
        server_key=server_half, passphrase_hash=None, track=False, expires_in=-60,
    )
    encoded = crypto.encode_half(client_half)
    resp = client.post(
        f"/s/{r['token']}/reveal", json={"key": encoded}, headers={"Origin": "http://testserver"}
    )
    assert resp.status_code == 404
