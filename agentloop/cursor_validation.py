"""Cursor encoding and validation shared by the storage backends."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

_VALID_CURSOR_ARITIES = frozenset({2, 3})


class InvalidCursorError(ValueError):
    """Raised when a pagination cursor cannot be decoded."""


def encode_cursor(parts: list[Any]) -> str:
    raw = json.dumps(parts, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, *, expected_parts: int | None = None) -> list[str]:
    """Decode a pagination cursor and reject malformed payload shapes."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        parts: Any = json.loads(raw)
    except (
        binascii.Error,
        UnicodeDecodeError,
        UnicodeEncodeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise InvalidCursorError(f"invalid pagination cursor: {cursor!r}") from exc

    if not isinstance(parts, list):
        raise InvalidCursorError(f"invalid pagination cursor: {cursor!r}")
    valid_arity = (
        len(parts) == expected_parts
        if expected_parts is not None
        else len(parts) in _VALID_CURSOR_ARITIES
    )
    if not valid_arity:
        raise InvalidCursorError(f"invalid pagination cursor: {cursor!r}")
    if any(not isinstance(part, str) or not part for part in parts):
        raise InvalidCursorError(f"invalid pagination cursor: {cursor!r}")
    return parts
