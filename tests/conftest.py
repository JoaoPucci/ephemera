"""Shared fixtures for the ephemera test suite."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def tmp_db_path(tmp_path, monkeypatch):
    """Isolated SQLite DB file, wired in via env vars and settings cache reset."""
    db = tmp_path / "test.db"
    monkeypatch.setenv("EPHEMERA_DB_PATH", str(db))
    monkeypatch.setenv("EPHEMERA_API_KEY", "test-api-key")
    monkeypatch.setenv("EPHEMERA_SECRET_KEY", "test-secret-key-abcdef0123456789")
    monkeypatch.setenv("EPHEMERA_BASE_URL", "http://testserver")
    monkeypatch.setenv("EPHEMERA_ALLOWED_ORIGINS", "http://testserver")

    from app import config

    config.get_settings.cache_clear()

    from app import models

    models.init_db()
    yield db
    config.get_settings.cache_clear()


@pytest.fixture
def client(tmp_db_path):
    """FastAPI TestClient bound to an isolated DB, with rate-limiter reset."""
    from fastapi.testclient import TestClient
    from app import create_app
    from app.limiter import reveal_limiter

    reveal_limiter.reset()
    app = create_app()
    with TestClient(app) as c:
        yield c
    reveal_limiter.reset()


@pytest.fixture
def api_key():
    return "test-api-key"


@pytest.fixture
def auth_headers(api_key):
    return {"Authorization": f"Bearer {api_key}", "Origin": "http://testserver"}


@pytest.fixture
def sample_png_bytes():
    # Minimal valid PNG: 1x1 transparent
    import base64

    b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        "+P+/HgAFhAJ/wlseKgAAAABJRU5ErkJggg=="
    )
    return base64.b64decode(b64)


@pytest.fixture
def sample_jpeg_bytes():
    # Minimal JPEG header (not a full valid image, but starts with correct magic)
    return bytes.fromhex("ffd8ffe000104a46494600010101006000600000") + b"\x00" * 32 + bytes.fromhex("ffd9")


@pytest.fixture
def sample_gif_bytes():
    return b"GIF89a" + b"\x01\x00\x01\x00\x00\x00\x00;"


@pytest.fixture
def sample_webp_bytes():
    return b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 24


@pytest.fixture
def sample_svg_bytes():
    return b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>'
