"""Shared fixtures for the ephemera test suite."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


TEST_USERNAME = "alice"
TEST_PASSWORD = "test-password-xyz"


@pytest.fixture
def tmp_db_path(tmp_path, monkeypatch):
    """Isolated SQLite DB file, wired in via env vars and settings cache reset."""
    db = tmp_path / "test.db"
    monkeypatch.setenv("EPHEMERA_DB_PATH", str(db))
    monkeypatch.setenv("EPHEMERA_SECRET_KEY", "test-secret-key-abcdef0123456789")
    monkeypatch.setenv("EPHEMERA_BASE_URL", "http://testserver")
    monkeypatch.setenv("EPHEMERA_ALLOWED_ORIGINS", "http://testserver")
    # TestClient requests are http://; the Secure flag would prevent the
    # session cookie from round-tripping. Production default is True.
    monkeypatch.setenv("EPHEMERA_SESSION_COOKIE_SECURE", "false")
    # Explicitly pin the prod posture so tests on a dev box that has
    # EPHEMERA_DEPLOYMENT_LABEL set in its .env (the typical dev-vs-prod
    # distinguishability case the setting exists for) don't flip the
    # default-posture assertions in tests/test_pwa.py. Tests that need
    # the dev posture override this via their own fixture.
    monkeypatch.setenv("EPHEMERA_DEPLOYMENT_LABEL", "")

    from app import config

    config.get_settings.cache_clear()

    from app import models

    models.init_db()
    yield db
    config.get_settings.cache_clear()


def _provision(username: str, password: str = TEST_PASSWORD) -> dict:
    """Create a user directly via the data layer. Returns a dict with the
    created user's id, password, totp_secret, and a pyotp.TOTP helper."""
    import pyotp

    from app import auth, models

    secret = pyotp.random_base32(length=32)
    _codes, codes_json = auth.generate_recovery_codes()
    uid = models.create_user(
        username=username,
        password_hash=auth.hash_password(password),
        totp_secret=secret,
        recovery_code_hashes=codes_json,
    )
    return {
        "id": uid,
        "username": username,
        "password": password,
        "totp_secret": secret,
        "totp": pyotp.TOTP(
            secret, digits=auth.TOTP_DIGITS, interval=auth.TOTP_INTERVAL
        ),
    }


@pytest.fixture
def provisioned_user(tmp_db_path):
    """Default single user for tests that don't need multi-user coverage."""
    return _provision(TEST_USERNAME)


@pytest.fixture
def make_user(tmp_db_path):
    """Factory for creating additional users on demand (multi-user tests)."""

    def _make(username: str, password: str = TEST_PASSWORD) -> dict:
        return _provision(username, password)

    return _make


@pytest.fixture
def api_token(provisioned_user):
    """Mint a test API token bound to the default provisioned user."""
    from app import auth, models

    plaintext, digest = auth.mint_api_token()
    models.create_token(user_id=provisioned_user["id"], name="test", token_hash=digest)
    return plaintext


@pytest.fixture
def client(tmp_db_path):
    """FastAPI TestClient bound to an isolated DB, with rate-limiters reset."""
    from fastapi.testclient import TestClient

    from app import create_app
    from app.limiter import create_limiter, login_limiter, read_limiter, reveal_limiter

    for lim in (reveal_limiter, login_limiter, create_limiter, read_limiter):
        lim.reset()
    app = create_app()
    with TestClient(app) as c:
        yield c
    for lim in (reveal_limiter, login_limiter, create_limiter, read_limiter):
        lim.reset()


@pytest.fixture
def authed_client(client, provisioned_user):
    """A TestClient already logged in as the default provisioned user."""
    code = provisioned_user["totp"].now()
    r = client.post(
        "/send/login",
        data={
            "username": provisioned_user["username"],
            "password": provisioned_user["password"],
            "code": code,
        },
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200, r.text
    return client


@pytest.fixture
def auth_headers(api_token):
    """Bearer-token headers for API routes (replaces the old static API key)."""
    return {"Authorization": f"Bearer {api_token}", "Origin": "http://testserver"}


@pytest.fixture
def sample_png_bytes():
    import base64

    b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        "+P+/HgAFhAJ/wlseKgAAAABJRU5ErkJggg=="
    )
    return base64.b64decode(b64)


@pytest.fixture
def sample_jpeg_bytes():
    return (
        bytes.fromhex("ffd8ffe000104a46494600010101006000600000")
        + b"\x00" * 32
        + bytes.fromhex("ffd9")
    )


@pytest.fixture
def sample_gif_bytes():
    return b"GIF89a" + b"\x01\x00\x01\x00\x00\x00\x00;"


@pytest.fixture
def sample_webp_bytes():
    return b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 24


@pytest.fixture
def sample_svg_bytes():
    return b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>'
