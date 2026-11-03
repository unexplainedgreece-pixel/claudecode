"""
AES-256-GCM encryption for sensitive values (API tokens).

The key comes from APP_SECRET_KEY in .env: a 32-byte key encoded as
URL-safe base64. Generate one with:

    python3 -m src.security.crypto keygen

What this protects: the encrypted values stored in the database are useless
without the key, so a copied or backed-up scheduler.db does not leak tokens.
What it does NOT protect: anyone who can read both .env and the database on
this machine can decrypt everything. Keep .env out of git and backups.
"""

import base64
import os
import sys

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_ENV_VAR = "APP_SECRET_KEY"
KEY_BYTES = 32           # 256-bit key
NONCE_BYTES = 12         # GCM standard nonce size
VERSION_PREFIX = "v1:"   # lets us change the scheme later without breaking old data


class CryptoError(Exception):
    """Raised when the key is missing/invalid or a value cannot be decrypted."""


def generate_key() -> str:
    """Return a new random key, base64 encoded, ready to paste into .env."""
    return base64.urlsafe_b64encode(os.urandom(KEY_BYTES)).decode()


def load_key(encoded: str | None = None) -> bytes:
    """Decode the key from the argument or from APP_SECRET_KEY. Raise if unusable."""
    encoded = (encoded if encoded is not None else os.getenv(KEY_ENV_VAR) or "").strip()
    if not encoded or encoded.startswith("your_"):
        raise CryptoError(
            f"{KEY_ENV_VAR} is not set. Run `python3 -m src.security.crypto keygen` "
            "and put the result in .env."
        )
    try:
        key = base64.urlsafe_b64decode(encoded)
    except (ValueError, TypeError) as exc:
        raise CryptoError(f"{KEY_ENV_VAR} is not valid base64.") from exc
    if len(key) != KEY_BYTES:
        raise CryptoError(f"{KEY_ENV_VAR} must decode to {KEY_BYTES} bytes, got {len(key)}.")
    return key


def encrypt(plaintext: str, key: bytes | None = None) -> str:
    """Encrypt a string. Returns 'v1:<base64 nonce+ciphertext+tag>'."""
    key = key or load_key()
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return VERSION_PREFIX + base64.urlsafe_b64encode(nonce + ciphertext).decode()


def decrypt(token: str, key: bytes | None = None) -> str:
    """Reverse encrypt(). Raises CryptoError on a wrong key or tampered data."""
    key = key or load_key()
    if not token.startswith(VERSION_PREFIX):
        raise CryptoError("Value is not in a recognised encrypted format.")
    try:
        raw = base64.urlsafe_b64decode(token[len(VERSION_PREFIX):])
        nonce, ciphertext = raw[:NONCE_BYTES], raw[NONCE_BYTES:]
        return AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8")
    except InvalidTag as exc:
        raise CryptoError("Decryption failed: wrong APP_SECRET_KEY or corrupted data.") from exc
    except (ValueError, TypeError) as exc:
        raise CryptoError(f"Decryption failed: {exc}") from exc


def is_encrypted(value: str) -> bool:
    return isinstance(value, str) and value.startswith(VERSION_PREFIX)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["keygen"]:
        print(f"{KEY_ENV_VAR}={generate_key()}")
        print("\nAdd the line above to your .env file. Never commit it to git.", file=sys.stderr)
        return 0
    print("Usage: python3 -m src.security.crypto keygen", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
