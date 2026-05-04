"""Tests for app.crypto: key generation, splitting, encryption round-trips."""

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app import crypto


def test_generate_key_returns_32_bytes() -> None:
    key = crypto.generate_key()
    assert isinstance(key, bytes)
    assert len(key) == 32


def test_split_key_returns_two_16_byte_halves() -> None:
    key = crypto.generate_key()
    server_half, client_half = crypto.split_key(key)
    assert len(server_half) == 16
    assert len(client_half) == 16
    assert server_half + client_half == key


def test_reconstruct_key_from_halves_matches_original() -> None:
    key = crypto.generate_key()
    server_half, client_half = crypto.split_key(key)
    assert crypto.reconstruct_key(server_half, client_half) == key


def test_client_half_encoded_is_urlsafe_string() -> None:
    key = crypto.generate_key()
    _, client_half = crypto.split_key(key)
    encoded = crypto.encode_half(client_half)
    assert isinstance(encoded, str)
    assert "=" not in encoded  # no padding
    assert "+" not in encoded and "/" not in encoded
    assert crypto.decode_half(encoded) == client_half


def test_encrypt_then_decrypt_text_roundtrip() -> None:
    key = crypto.generate_key()
    plaintext = "hello, ephemera — a secret message".encode()
    ciphertext = crypto.encrypt(plaintext, key)
    assert ciphertext != plaintext
    assert crypto.decrypt(ciphertext, key) == plaintext


def test_encrypt_then_decrypt_binary_roundtrip() -> None:
    key = crypto.generate_key()
    blob = bytes(range(256)) * 10
    ciphertext = crypto.encrypt(blob, key)
    assert crypto.decrypt(ciphertext, key) == blob


def test_decrypt_with_wrong_key_raises() -> None:
    key_a = crypto.generate_key()
    key_b = crypto.generate_key()
    ciphertext = crypto.encrypt(b"secret", key_a)
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(ciphertext, key_b)


def test_decrypt_with_corrupted_ciphertext_raises() -> None:
    key = crypto.generate_key()
    ciphertext = bytearray(crypto.encrypt(b"secret", key))
    ciphertext[10] ^= 0xFF
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(bytes(ciphertext), key)


def test_decrypt_with_wrong_client_half_raises() -> None:
    key = crypto.generate_key()
    server_half, _ = crypto.split_key(key)
    bad_client = b"\x00" * 16
    reconstructed = crypto.reconstruct_key(server_half, bad_client)
    ciphertext = crypto.encrypt(b"secret", key)
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(ciphertext, reconstructed)


def test_two_generated_keys_differ() -> None:
    keys = {crypto.generate_key() for _ in range(50)}
    assert len(keys) == 50


def test_split_key_rejects_wrong_size() -> None:
    """split_key demands exactly KEY_SIZE bytes -- catches callers that pass
    an already-halved key or some other blob."""
    import pytest

    with pytest.raises(ValueError):
        crypto.split_key(b"\x00" * 16)  # too short
    with pytest.raises(ValueError):
        crypto.split_key(b"\x00" * 64)  # too long


def test_reconstruct_key_rejects_wrong_half_sizes() -> None:
    """reconstruct_key demands exactly HALF_SIZE bytes on each side so a
    silent "oops I concatenated something else" produces a key we actively
    reject rather than a 32-byte thing that just decrypts to garbage."""
    import pytest

    with pytest.raises(ValueError):
        crypto.reconstruct_key(b"\x00" * 8, b"\x00" * 16)
    with pytest.raises(ValueError):
        crypto.reconstruct_key(b"\x00" * 16, b"\x00" * 8)


# ---------------------------------------------------------------------------
# At-rest helpers
# ---------------------------------------------------------------------------


def test_encrypt_at_rest_roundtrips(tmp_db_path: Path) -> None:
    """tmp_db_path indirectly sets EPHEMERA_SECRET_KEY via the fixture."""
    token = crypto.encrypt_at_rest("JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP")
    assert token.startswith("v1:")
    assert crypto.decrypt_at_rest(token) == "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


def test_encrypt_at_rest_produces_distinct_ciphertexts_for_same_input(tmp_db_path: Path) -> None:
    """Fernet embeds a random IV, so two encryptions of the same plaintext
    must differ -- otherwise an attacker can tell which users share a secret."""
    a = crypto.encrypt_at_rest("same-plaintext")
    b = crypto.encrypt_at_rest("same-plaintext")
    assert a != b
    assert crypto.decrypt_at_rest(a) == crypto.decrypt_at_rest(b) == "same-plaintext"


def test_decrypt_at_rest_rejects_non_v1_string(tmp_db_path: Path) -> None:
    """is_at_rest_ciphertext gates migration detection -- a bare plaintext
    string must NEVER be treated as a decryptable token."""
    import pytest

    with pytest.raises(crypto.AtRestDecryptionError):
        crypto.decrypt_at_rest("JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP")


def test_decrypt_at_rest_fails_after_secret_key_rotation(tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rotating EPHEMERA_SECRET_KEY makes existing ciphertexts unreadable.
    This is the documented operator cost of at-rest encryption; the test
    pins that the failure is a loud AtRestDecryptionError, not a silent
    wrong value."""
    import pytest

    from app import config

    token = crypto.encrypt_at_rest("JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP")
    monkeypatch.setenv("EPHEMERA_SECRET_KEY", "a-completely-different-key-0123456789")
    config.get_settings.cache_clear()
    try:
        with pytest.raises(crypto.AtRestDecryptionError):
            crypto.decrypt_at_rest(token)
    finally:
        config.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Property-based tests
#
# Where the example tests above pin specific shapes (32-byte key, ascii
# plaintext, particular ciphertext format), these properties pin the
# *invariants* the crypto module is supposed to hold for any input. The
# hypothesis framework generates random inputs against the property; a
# failure surfaces a counter-example that breaks the invariant.
#
# Two reasons this matters more in AI-assisted development than otherwise:
#   1. example tests written alongside the implementation tend to assert
#      what the implementation does, not what it should do. A property
#      describes the contract independently of the implementation.
#   2. crypto round-trips are exactly the kind of code where edge-case
#      misses (empty input, max-length input, NUL bytes, inputs that
#      collide with the framing layer's escape characters) are
#      catastrophic and rarely caught by hand-picked examples.
# ---------------------------------------------------------------------------


@given(plaintext=st.binary(min_size=0, max_size=512))
@settings(max_examples=100)
def test_property_encrypt_decrypt_roundtrip_on_any_bytes(plaintext: bytes) -> None:
    """For any bytes value (including empty, NUL-laden, and full byte
    range), encrypt-then-decrypt with a freshly-generated key returns
    the original. Checks the Fernet framing layer survives every input
    we could plausibly encrypt: text content, image bytes, etc."""
    key = crypto.generate_key()
    assert crypto.decrypt(crypto.encrypt(plaintext, key), key) == plaintext


@given(
    plaintext=st.binary(min_size=1, max_size=512),
    wrong_key=st.binary(min_size=32, max_size=32),
)
@settings(max_examples=50)
def test_property_decrypt_with_unrelated_key_raises(plaintext: bytes, wrong_key: bytes) -> None:
    """For any plaintext encrypted under one key, decrypting under any
    OTHER 32-byte key raises DecryptionError. Pins that the failure
    mode is an exception (loud) rather than silent wrong-bytes (which
    would defeat the integrity guarantee Fernet provides). Also covers
    the case where the two keys happen to share a prefix or other
    structure -- hypothesis will hand-craft adversarial pairs."""
    encrypt_key = crypto.generate_key()
    # Filter out the astronomically-unlikely collision with the
    # generated key. hypothesis.assume() drops the example cleanly.
    if wrong_key == encrypt_key:
        return
    ciphertext = crypto.encrypt(plaintext, encrypt_key)
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(ciphertext, wrong_key)
