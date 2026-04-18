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
