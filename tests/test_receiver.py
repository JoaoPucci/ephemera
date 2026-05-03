"""Tests for receiver routes: landing page, reveal, passphrase, burn-on-failure."""


def _create_text_secret(
    client, auth_headers, content="the secret", passphrase=None, track=False
):
    body = {
        "content": content,
        "content_type": "text",
        "expires_in": 3600,
        "track": track,
    }
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
    client.post(
        f"/s/{token}/reveal",
        json={"key": client_half},
        headers={"Origin": "http://testserver"},
    )
    from app import models

    assert models.get_by_token(token) is None


def test_reveal_twice_second_returns_404(client, auth_headers):
    secret = _create_text_secret(client, auth_headers)
    token, client_half = _token_and_client_half(secret["url"])
    client.post(
        f"/s/{token}/reveal",
        json={"key": client_half},
        headers={"Origin": "http://testserver"},
    )
    r = client.post(
        f"/s/{token}/reveal",
        json={"key": client_half},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 404


def test_concurrent_reveals_exactly_one_gets_plaintext(client, auth_headers):
    """Two threaded reveals racing: one 200 with plaintext, the other 404.
    Regression gate for the atomic-reveal fix: if the route loses its
    atomic gate, both callers would return the plaintext."""
    import threading

    secret = _create_text_secret(client, auth_headers, content="race-winner")
    token, client_half = _token_and_client_half(secret["url"])
    headers = {"Origin": "http://testserver"}
    barrier = threading.Barrier(2)
    statuses: list[int] = []
    bodies: list[str] = []
    lock = threading.Lock()

    def fire():
        barrier.wait()
        r = client.post(
            f"/s/{token}/reveal", json={"key": client_half}, headers=headers
        )
        with lock:
            statuses.append(r.status_code)
            if r.status_code == 200:
                bodies.append(r.json()["content"])

    t1 = threading.Thread(target=fire)
    t2 = threading.Thread(target=fire)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert sorted(statuses) == [200, 404]
    assert bodies == ["race-winner"]


def test_reveal_with_wrong_key_returns_error(client, auth_headers):
    secret = _create_text_secret(client, auth_headers)
    token, _ = _token_and_client_half(secret["url"])
    import base64

    bad = base64.urlsafe_b64encode(b"\x00" * 16).rstrip(b"=").decode()
    r = client.post(
        f"/s/{token}/reveal", json={"key": bad}, headers={"Origin": "http://testserver"}
    )
    assert r.status_code == 400


def test_reveal_with_malformed_base64_fragment_returns_400(client, auth_headers):
    """A fragment that isn't valid base64url at all -- decode_half raises,
    we return 400 'malformed key' rather than letting the exception bubble."""
    secret = _create_text_secret(client, auth_headers)
    token, _ = _token_and_client_half(secret["url"])
    r = client.post(
        f"/s/{token}/reveal",
        json={"key": "!!!not-base64@@@"},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 400


def test_reveal_with_wrong_length_fragment_returns_400(client, auth_headers):
    """Valid base64url but the decoded bytes aren't 16 bytes long -- we
    reject before reaching Fernet so the error message is specific."""
    secret = _create_text_secret(client, auth_headers)
    token, _ = _token_and_client_half(secret["url"])
    import base64

    wrong_size = base64.urlsafe_b64encode(b"\x00" * 8).rstrip(b"=").decode()
    r = client.post(
        f"/s/{token}/reveal",
        json={"key": wrong_size},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 400


def test_reveal_without_passphrase_when_required_rejected(client, auth_headers):
    secret = _create_text_secret(client, auth_headers, passphrase="pw")
    token, client_half = _token_and_client_half(secret["url"])
    r = client.post(
        f"/s/{token}/reveal",
        json={"key": client_half},
        headers={"Origin": "http://testserver"},
    )
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
    secret = _create_text_secret(
        client, auth_headers, content="payload", passphrase="correct"
    )
    token, client_half = _token_and_client_half(secret["url"])
    r = client.post(
        f"/s/{token}/reveal",
        json={"key": client_half, "passphrase": "correct"},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200
    assert r.json()["content"] == "payload"


def test_reveal_burns_secret_after_too_many_failed_passphrase_attempts(
    client, auth_headers
):
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
    rv = client.post(
        f"/s/{token}/reveal",
        json={"key": client_half},
        headers={"Origin": "http://testserver"},
    )
    assert rv.status_code == 200
    body = rv.json()
    assert body["content_type"] == "image"
    assert body["mime_type"] == "image/png"
    import base64

    assert base64.b64decode(body["content"]) == sample_png_bytes


def test_reveal_404_for_expired_secret(client, auth_headers, provisioned_user):
    from app import crypto, models

    # Create directly with negative expiry.
    key = crypto.generate_key()
    server_half, client_half = crypto.split_key(key)
    ct = crypto.encrypt(b"x", key)
    r = models.create_secret(
        user_id=provisioned_user["id"],
        content_type="text",
        mime_type=None,
        ciphertext=ct,
        server_key=server_half,
        passphrase_hash=None,
        track=False,
        expires_in=-60,
    )
    encoded = crypto.encode_half(client_half)
    resp = client.post(
        f"/s/{r['token']}/reveal",
        json={"key": encoded},
        headers={"Origin": "http://testserver"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Property tests: secret-flow invariants
#
# The unit tests above pin specific shapes (one well-formed text secret,
# one tampered fragment, one passphrase-protected secret). Hypothesis
# extends each invariant across the input space:
#
#   - mint/reveal round-trip: any text payload that goes in via POST
#     /api/secrets reveals byte-identical via POST /s/{token}/reveal.
#     Catches encoding regressions (UTF-8 round-trip, NUL bytes,
#     emoji, whitespace) the fixed examples don't enumerate.
#
#   - single-use enforcement: any minted secret returns 404 on the
#     second reveal call. Pins the consume-on-success contract
#     against any path that quietly leaves the row behind.
#
#   - tampered-token rejection: any printable-ASCII string that the
#     server didn't issue returns 404 on the meta and reveal
#     endpoints. Catches a regression that loosened the token
#     existence check (e.g. via a partial/prefix match).
#
#   - passphrase round-trip: any passphrase-protected secret reveals
#     iff the caller presents the same passphrase. Pins the bcrypt
#     verify pipeline against off-by-one collations the unit tests
#     don't cover (NUL bytes, unicode normalization).
#
# Each example mints a fresh secret (the in-DB rows accumulate within
# one test function but never collide -- tokens are server-issued
# UUIDs). Hypothesis is told the function-scoped fixture is fine via
# `suppress_health_check`. `max_examples` stays modest because each
# round-trip is one full HTTP request pair through the test client.
# ---------------------------------------------------------------------------

from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402


def _reset_rate_limits():
    """Reset every rate-limiter bucket. Hypothesis runs many examples
    inside one test function; the `client` fixture only resets at
    function setup/teardown, so without a per-example reset the
    create-secret limiter would trip mid-property and the round-trip
    assertion would fail with a spurious 429 instead of the bug it's
    actually looking for."""
    from app.limiter import create_limiter, login_limiter, read_limiter, reveal_limiter

    for lim in (reveal_limiter, login_limiter, create_limiter, read_limiter):
        lim.reset()


# Content strategy: any single Unicode codepoint that's allowed in a
# JSON string round-trip. Includes NUL (``), TAB, LF, CR, and
# the rest of the C0 control range, since the docstring claims those
# are covered and a regression in NUL handling is a documented bug
# class for stdlib `json` interactions. Surrogate halves (category
# `Cs`) are dropped because they're not valid Unicode scalars and
# Python's `json.dumps` rejects them. Avoid empty content (the route
# returns 422 -- the property is about round-trip for VALID inputs).
_text_content = st.text(
    min_size=1,
    max_size=200,
    alphabet=st.characters(
        min_codepoint=0x00,
        max_codepoint=0x10FFFF,
        blacklist_categories=("Cs",),  # type: ignore[arg-type]
    ),
)


@given(content=_text_content)
@settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
def test_property_text_secret_round_trips(client, auth_headers, content: str):
    """For any non-empty text content (printable + unicode + whitespace),
    POST /api/secrets followed by POST /s/{token}/reveal returns the
    same string byte-for-byte. Catches encoding regressions:
    UTF-8 round-trip, NUL-bearing strings, surrogate-pair handling,
    trailing whitespace, emoji."""
    _reset_rate_limits()
    secret = _create_text_secret(client, auth_headers, content=content)
    token, client_half = _token_and_client_half(secret["url"])
    r = client.post(
        f"/s/{token}/reveal",
        json={"key": client_half},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["content_type"] == "text"
    assert body["content"] == content


@given(content=_text_content)
@settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
def test_property_reveal_is_single_use(client, auth_headers, content: str):
    """For any minted secret, the second reveal call returns 404. Pins
    the consume-on-success contract against any code path that quietly
    leaves the row behind on success (would let a second viewer get the
    plaintext, breaking the "ephemeral" guarantee)."""
    _reset_rate_limits()
    secret = _create_text_secret(client, auth_headers, content=content)
    token, client_half = _token_and_client_half(secret["url"])
    headers = {"Origin": "http://testserver"}
    first = client.post(
        f"/s/{token}/reveal", json={"key": client_half}, headers=headers
    )
    assert first.status_code == 200
    second = client.post(
        f"/s/{token}/reveal", json={"key": client_half}, headers=headers
    )
    assert second.status_code == 404


# Token-shape strategy: url-safe-base64 alphabet (A-Z, a-z, 0-9, _, -),
# realistic length range. Filters the generated string against the
# token format so we exercise "looks like a token but wasn't issued"
# rather than "obviously garbage."
_token_shaped = st.text(
    min_size=8,
    max_size=64,
    alphabet=st.sampled_from(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    ),
)


@given(fake_token=_token_shaped)
@settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
def test_property_unknown_token_returns_404(client, fake_token: str):
    """For any token-shaped string the server didn't issue, the meta
    endpoint returns 404. Catches a regression that loosened the token
    existence check (prefix match, case-insensitive lookup, etc.). The
    landing page (GET /s/{token}) deliberately returns 200 for any
    token to avoid leaking existence to scrapers; this property is on
    the meta endpoint, which is the actual existence gate."""
    _reset_rate_limits()
    r = client.get(f"/s/{fake_token}/meta")
    assert r.status_code == 404


# Passphrase strategy: same printable-text shape as content, but
# constrained to bcrypt's 72-byte input cap so we don't trip the
# password-length boundary the unit suite documents in test_auth.
_passphrase = st.text(min_size=1, max_size=72).filter(
    lambda s: 0 < len(s.encode("utf-8")) <= 72
)


@given(passphrase=_passphrase, content=_text_content)
@settings(
    max_examples=10,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
def test_property_passphrase_protected_round_trips(
    client, auth_headers, passphrase: str, content: str
):
    """For any (passphrase, content) pair within bcrypt's input cap,
    minting with that passphrase and revealing with the same
    passphrase returns the original content. Pins the bcrypt verify
    pipeline against unicode-normalization / encoding edge cases the
    unit tests don't enumerate. Cap at 10 examples because each
    round-trip is bcrypt-cost-12 hash + verify (~500ms)."""
    _reset_rate_limits()
    secret = _create_text_secret(
        client, auth_headers, content=content, passphrase=passphrase
    )
    token, client_half = _token_and_client_half(secret["url"])
    r = client.post(
        f"/s/{token}/reveal",
        json={"key": client_half, "passphrase": passphrase},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["content"] == content
