"""Key generation, splitting, encryption, and decryption for ephemera."""
import base64
import os

from cryptography.fernet import Fernet, InvalidToken


KEY_SIZE = 32
HALF_SIZE = KEY_SIZE // 2


class DecryptionError(Exception):
    """Raised when decryption fails for any reason (wrong key, tampering, expiry)."""


def generate_key() -> bytes:
    return os.urandom(KEY_SIZE)


def split_key(key: bytes) -> tuple[bytes, bytes]:
    if len(key) != KEY_SIZE:
        raise ValueError(f"key must be {KEY_SIZE} bytes")
    return key[:HALF_SIZE], key[HALF_SIZE:]


def reconstruct_key(server_half: bytes, client_half: bytes) -> bytes:
    if len(server_half) != HALF_SIZE or len(client_half) != HALF_SIZE:
        raise ValueError("each half must be 16 bytes")
    return server_half + client_half


def encode_half(half: bytes) -> str:
    return base64.urlsafe_b64encode(half).rstrip(b"=").decode("ascii")


def decode_half(s: str) -> bytes:
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


def _fernet(key: bytes) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(data: bytes, key: bytes) -> bytes:
    return _fernet(key).encrypt(data)


def decrypt(ciphertext: bytes, key: bytes) -> bytes:
    try:
        return _fernet(key).decrypt(ciphertext)
    except InvalidToken as e:
        raise DecryptionError("decryption failed") from e
    except Exception as e:  # malformed input, wrong length, etc.
        raise DecryptionError("decryption failed") from e
