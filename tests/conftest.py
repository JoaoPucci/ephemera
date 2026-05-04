"""Shared fixtures for the ephemera test suite."""

import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

# Drop bcrypt's cost factor in tests via an explicit env-var signal
# that `app/auth/_core.py` reads at import time. Cost-12 hashing makes
# the suite ~10min wall-clock, which times out cosmic-ray's per-mutant
# runs on the GitHub-hosted 6h ceiling. Cost-4 hashing makes the same
# suite finish in ~15s while preserving every constant-time assertion
# (those count `bcrypt.checkpw` invocations via monkeypatch, not
# wall-clock).
#
# `setdefault` so a developer can override the override -- e.g.
# `EPHEMERA_TEST_BCRYPT_ROUNDS_OVERRIDE=12 pytest` to validate at
# production cost.
#
# Set before any `from app...` import below so `_core.py`'s module-
# level read of the env var sees this value.
os.environ.setdefault("EPHEMERA_TEST_BCRYPT_ROUNDS_OVERRIDE", "4")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


TEST_USERNAME = "alice"
TEST_PASSWORD = "test-password-xyz"


@pytest.fixture(scope="session", autouse=True)
def _verify_bcrypt_test_override_applied() -> None:
    """Session-level safety net for the bcrypt test-mode override.

    Conftest's module-level `os.environ.setdefault` above sets
    `EPHEMERA_TEST_BCRYPT_ROUNDS_OVERRIDE=4`; `app/auth/_core.py`
    reads it at import time and rebinds `BCRYPT_ROUNDS` to 4. A
    mutation that disables the override gate -- flipping the
    `if _test_override:` condition, blanking the
    `os.environ.get(...)` read, deleting the assignment line --
    silently reverts to cost-12. The suite still passes, just
    very slowly; cosmic-ray would flag the mutation as SURVIVED
    only after a 19-min cost-12 run per affected mutant.

    Asserting at session start instead of inside a regular test
    means cosmic-ray declares such mutants KILLED at <1s instead
    of waiting ~2min for pytest's collection order to reach
    `test_auth.py`. Empirically: the post-#133 weekly run
    (run 25267652770) had one cost-4-gate mutation surviving for
    124.7s before the in-test assertion fired; with this fixture
    the same mutation is killed by the first attempted test's
    fixture setup.

    Pins the runtime-resolved value, complementing
    `test_security_constants_are_not_silently_weakened` in
    `test_fitness_functions.py` (which AST-pins the source-level
    `BCRYPT_ROUNDS = 12` literal). Different question, different
    layer: source pin guarantees production cost; this pin
    guarantees the test-mode override is wired correctly."""
    from app.auth._core import BCRYPT_ROUNDS

    override = os.environ.get("EPHEMERA_TEST_BCRYPT_ROUNDS_OVERRIDE")
    assert override, (
        "EPHEMERA_TEST_BCRYPT_ROUNDS_OVERRIDE not set -- the "
        "module-level `os.environ.setdefault` above this fixture "
        "is expected to set it before any test runs."
    )
    assert int(override) == BCRYPT_ROUNDS, (
        f"BCRYPT_ROUNDS = {BCRYPT_ROUNDS}, expected {override}; "
        "the override gate in app/auth/_core.py is not applying. "
        "If a mutation flipped the gate condition, this fixture "
        "kills it in milliseconds instead of surviving a 19-min "
        "cost-12 suite run."
    )


@pytest.fixture
def tmp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
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


def _provision(username: str, password: str = TEST_PASSWORD) -> dict[str, Any]:
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
def provisioned_user(tmp_db_path: Path) -> dict[str, Any]:
    """Default single user for tests that don't need multi-user coverage."""
    return _provision(TEST_USERNAME)


@pytest.fixture
def make_user(
    tmp_db_path: Path,
) -> Callable[..., dict[str, Any]]:
    """Factory for creating additional users on demand (multi-user tests)."""

    def _make(username: str, password: str = TEST_PASSWORD) -> dict[str, Any]:
        return _provision(username, password)

    return _make


@pytest.fixture
def api_token(provisioned_user: dict[str, Any]) -> str:
    """Mint a test API token bound to the default provisioned user."""
    from app import auth, models

    plaintext, digest = auth.mint_api_token()
    models.create_token(user_id=provisioned_user["id"], name="test", token_hash=digest)
    return plaintext


@pytest.fixture
def client(tmp_db_path: Path) -> Iterator[TestClient]:
    """FastAPI TestClient bound to an isolated DB, with rate-limiters reset."""
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
def authed_client(client: TestClient, provisioned_user: dict[str, Any]) -> TestClient:
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
def auth_headers(api_token: str) -> dict[str, str]:
    """Bearer-token headers for API routes (replaces the old static API key)."""
    return {"Authorization": f"Bearer {api_token}", "Origin": "http://testserver"}


@pytest.fixture
def sample_png_bytes() -> bytes:
    import base64

    b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        "+P+/HgAFhAJ/wlseKgAAAABJRU5ErkJggg=="
    )
    return base64.b64decode(b64)


@pytest.fixture
def sample_jpeg_bytes() -> bytes:
    return (
        bytes.fromhex("ffd8ffe000104a46494600010101006000600000")
        + b"\x00" * 32
        + bytes.fromhex("ffd9")
    )


@pytest.fixture
def sample_gif_bytes() -> bytes:
    return b"GIF89a" + b"\x01\x00\x01\x00\x00\x00\x00;"


@pytest.fixture
def sample_webp_bytes() -> bytes:
    return b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 24


@pytest.fixture
def sample_svg_bytes() -> bytes:
    return b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>'
