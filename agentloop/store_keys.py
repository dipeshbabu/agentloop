"""API key generation, hashing and verification for the trace stores.

Split out of ``agentloop.store`` (issue #407); ``agentloop.store`` re-exports
every public name, so existing imports keep working.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
from typing import Any

_API_KEY_PREFIX_LENGTH = 10
_API_KEY_SALT_BYTES = 16
_API_KEY_HASH_BYTES = 32
_API_KEY_SCRYPT_N = 2**14
_API_KEY_SCRYPT_R = 8
_API_KEY_SCRYPT_P = 1


def hash_api_key(api_key: str) -> str:
    salt = secrets.token_bytes(_API_KEY_SALT_BYTES)
    digest = hashlib.scrypt(
        api_key.encode("utf-8"),
        salt=salt,
        n=_API_KEY_SCRYPT_N,
        r=_API_KEY_SCRYPT_R,
        p=_API_KEY_SCRYPT_P,
        dklen=_API_KEY_HASH_BYTES,
    )
    return "scrypt$v1${salt}${digest}".format(
        salt=_encode_hash_bytes(salt),
        digest=_encode_hash_bytes(digest),
    )


def verify_api_key_hash(api_key: str, encoded_hash: str) -> bool:
    if not api_key.startswith("al_") or len(api_key) > 512:
        return False
    parts = encoded_hash.split("$")
    if len(parts) != 4 or parts[:2] != ["scrypt", "v1"]:
        return False
    try:
        salt = _decode_hash_bytes(parts[2])
        expected = _decode_hash_bytes(parts[3])
    except (binascii.Error, UnicodeEncodeError, ValueError):
        return False
    if len(salt) != _API_KEY_SALT_BYTES or len(expected) != _API_KEY_HASH_BYTES:
        return False
    actual = hashlib.scrypt(
        api_key.encode("utf-8"),
        salt=salt,
        n=_API_KEY_SCRYPT_N,
        r=_API_KEY_SCRYPT_R,
        p=_API_KEY_SCRYPT_P,
        dklen=_API_KEY_HASH_BYTES,
    )
    return hmac.compare_digest(actual, expected)


def _encode_hash_bytes(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_hash_bytes(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)


def _require_key_prefix(prefix: Any) -> None:
    if not isinstance(prefix, str) or len(prefix) != _API_KEY_PREFIX_LENGTH:
        raise ValueError(
            f"API key prefix must be the first {_API_KEY_PREFIX_LENGTH} characters of the key"
        )


def new_api_key() -> str:
    return "al_" + secrets.token_urlsafe(32)
