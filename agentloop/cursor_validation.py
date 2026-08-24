from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from agentloop.store import InvalidCursorError

_VALID_CURSOR_ARITIES = frozenset({2, 3})


def decode_cursor(cursor: str) -> list[str]:
    """Decode a stored pagination cursor and reject malformed payload shapes.

    Trace cursors contain two strings and finding cursors contain three. Keeping
    this validation at the shared decoder means both SQLite and Postgres fail
    with ``InvalidCursorError`` before either backend destructures the payload.
    """
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        parts: Any = json.loads(raw)
    except (binascii.Error, UnicodeDecodeError, UnicodeEncodeError, ValueError, json.JSONDecodeError) as exc:
        raise InvalidCursorError(f"invalid pagination cursor: {cursor!r}") from exc

    if not isinstance(parts, list) or len(parts) not in _VALID_CURSOR_ARITIES:
        raise InvalidCursorError(f"invalid pagination cursor: {cursor!r}")
    if any(not isinstance(part, str) or not part for part in parts):
        raise InvalidCursorError(f"invalid pagination cursor: {cursor!r}")
    return parts


def install_store_decoder() -> None:
    """Install the stricter shared decoder into the legacy store module."""
    import agentloop.store as store

    store.decode_cursor = decode_cursor
