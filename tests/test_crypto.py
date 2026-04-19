"""Tests for app.crypto: key generation, splitting, encryption round-trips."""
import pytest

from app import crypto


def test_generate_key_returns_32_bytes():
    key = crypto.generate_key()
    assert isinstance(key, bytes)
    assert len(key) == 32


def test_split_key_returns_two_16_byte_halves():
    key = crypto.generate_key()
    server_half, client_half = crypto.split_key(key)
    assert len(server_half) == 16
    assert len(client_half) == 16
    assert server_half + client_half == key


def test_reconstruct_key_from_halves_matches_original():
    key = crypto.generate_key()
    server_half, client_half = crypto.split_key(key)
    assert crypto.reconstruct_key(server_half, client_half) == key


def test_client_half_encoded_is_urlsafe_string():
    key = crypto.generate_key()
    _, client_half = crypto.split_key(key)
    encoded = crypto.encode_half(client_half)
    assert isinstance(encoded, str)
    assert "=" not in encoded  # no padding
    assert "+" not in encoded and "/" not in encoded
    assert crypto.decode_half(encoded) == client_half


def test_encrypt_then_decrypt_text_roundtrip():
    key = crypto.generate_key()
    plaintext = "hello, ephemera — a secret message".encode()
    ciphertext = crypto.encrypt(plaintext, key)
    assert ciphertext != plaintext
    assert crypto.decrypt(ciphertext, key) == plaintext


def test_encrypt_then_decrypt_binary_roundtrip():
    key = crypto.generate_key()
    blob = bytes(range(256)) * 10
    ciphertext = crypto.encrypt(blob, key)
    assert crypto.decrypt(ciphertext, key) == blob


def test_decrypt_with_wrong_key_raises():
    key_a = crypto.generate_key()
    key_b = crypto.generate_key()
    ciphertext = crypto.encrypt(b"secret", key_a)
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(ciphertext, key_b)


def test_decrypt_with_corrupted_ciphertext_raises():
    key = crypto.generate_key()
    ciphertext = bytearray(crypto.encrypt(b"secret", key))
    ciphertext[10] ^= 0xFF
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(bytes(ciphertext), key)


def test_decrypt_with_wrong_client_half_raises():
    key = crypto.generate_key()
    server_half, _ = crypto.split_key(key)
    bad_client = b"\x00" * 16
    reconstructed = crypto.reconstruct_key(server_half, bad_client)
    ciphertext = crypto.encrypt(b"secret", key)
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(ciphertext, reconstructed)


def test_two_generated_keys_differ():
    keys = {crypto.generate_key() for _ in range(50)}
    assert len(keys) == 50


def test_split_key_rejects_wrong_size():
    """split_key demands exactly KEY_SIZE bytes -- catches callers that pass
    an already-halved key or some other blob."""
    import pytest

    with pytest.raises(ValueError):
        crypto.split_key(b"\x00" * 16)   # too short
    with pytest.raises(ValueError):
        crypto.split_key(b"\x00" * 64)   # too long


def test_reconstruct_key_rejects_wrong_half_sizes():
    """reconstruct_key demands exactly HALF_SIZE bytes on each side so a
    silent "oops I concatenated something else" produces a key we actively
    reject rather than a 32-byte thing that just decrypts to garbage."""
    import pytest

    with pytest.raises(ValueError):
        crypto.reconstruct_key(b"\x00" * 8, b"\x00" * 16)
    with pytest.raises(ValueError):
        crypto.reconstruct_key(b"\x00" * 16, b"\x00" * 8)


# ---------------------------------------------------------------------------
# At-rest helpers (F-05)
# ---------------------------------------------------------------------------


def test_encrypt_at_rest_roundtrips(tmp_db_path):
    """tmp_db_path indirectly sets EPHEMERA_SECRET_KEY via the fixture."""
    token = crypto.encrypt_at_rest("JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP")
    assert token.startswith("v1:")
    assert crypto.decrypt_at_rest(token) == "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


def test_encrypt_at_rest_produces_distinct_ciphertexts_for_same_input(tmp_db_path):
    """Fernet embeds a random IV, so two encryptions of the same plaintext
    must differ -- otherwise an attacker can tell which users share a secret."""
    a = crypto.encrypt_at_rest("same-plaintext")
    b = crypto.encrypt_at_rest("same-plaintext")
    assert a != b
    assert crypto.decrypt_at_rest(a) == crypto.decrypt_at_rest(b) == "same-plaintext"


def test_decrypt_at_rest_rejects_non_v1_string(tmp_db_path):
    """is_at_rest_ciphertext gates migration detection -- a bare plaintext
    string must NEVER be treated as a decryptable token."""
    import pytest

    with pytest.raises(crypto.AtRestDecryptionError):
        crypto.decrypt_at_rest("JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP")


def test_decrypt_at_rest_fails_after_secret_key_rotation(tmp_db_path, monkeypatch):
    """Rotating EPHEMERA_SECRET_KEY makes existing ciphertexts unreadable.
    This is the documented operator cost of F-05; the test pins that the
    failure is a loud AtRestDecryptionError, not a silent wrong value."""
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
