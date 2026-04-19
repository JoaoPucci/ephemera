"""Key generation, splitting, encryption, and decryption for ephemera."""
import base64
import os

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


KEY_SIZE = 32
HALF_SIZE = KEY_SIZE // 2


class DecryptionError(Exception):
    """Raised when decryption fails for any reason (wrong key, tampering, expiry)."""


class AtRestDecryptionError(Exception):
    """Raised when an at-rest ciphertext cannot be decrypted (SECRET_KEY rotated
    after the value was written, or the DB row was tampered with)."""


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


# -----------------------------------------------------------------------------
# At-rest encryption for small DB-stored secrets (e.g. the TOTP seed, F-05).
#
# KEK is HKDF-derived from EPHEMERA_SECRET_KEY so operators don't need a second
# env var. Cost: rotating SECRET_KEY makes existing ciphertexts unreadable, so
# every user must re-run `rotate-totp`. That cost is documented in DEPLOYMENT.
# Stored values carry a version prefix ("v1:") so we can tell ciphertext from
# a legacy plaintext TOTP secret during the migration on init_db().
# -----------------------------------------------------------------------------

_AT_REST_VERSION = "v1:"
_AT_REST_INFO = b"ephemera-at-rest-kek-v1"


def _at_rest_key() -> bytes:
    """Derive the 32-byte KEK from EPHEMERA_SECRET_KEY. Local import of
    get_settings keeps app/config out of crypto.py's top-level import graph."""
    from .config import get_settings

    secret = get_settings().secret_key.encode("utf-8")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_SIZE,
        salt=None,
        info=_AT_REST_INFO,
    ).derive(secret)


def is_at_rest_ciphertext(s: str) -> bool:
    return isinstance(s, str) and s.startswith(_AT_REST_VERSION)


def encrypt_at_rest(plaintext: str) -> str:
    fernet = Fernet(base64.urlsafe_b64encode(_at_rest_key()))
    token = fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return _AT_REST_VERSION + token


def decrypt_at_rest(stored: str) -> str:
    if not is_at_rest_ciphertext(stored):
        raise AtRestDecryptionError("not an at-rest ciphertext")
    fernet = Fernet(base64.urlsafe_b64encode(_at_rest_key()))
    try:
        return fernet.decrypt(stored[len(_AT_REST_VERSION):].encode("ascii")).decode("utf-8")
    except InvalidToken as e:
        raise AtRestDecryptionError(
            "at-rest decryption failed (SECRET_KEY rotated or data tampered)"
        ) from e
