from __future__ import annotations

import base64
import binascii
import json
from functools import wraps
from inspect import signature
from typing import Any

from agentloop.store import InvalidCursorError

_VALID_CURSOR_ARITIES = frozenset({2, 3})


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

    allowed_arities = (
        frozenset({expected_parts}) if expected_parts is not None else _VALID_CURSOR_ARITIES
    )
    if not isinstance(parts, list) or len(parts) not in allowed_arities:
        raise InvalidCursorError(f"invalid pagination cursor: {cursor!r}")
    if any(not isinstance(part, str) or not part for part in parts):
        raise InvalidCursorError(f"invalid pagination cursor: {cursor!r}")
    return parts


def _page_validator(method, *, expected_parts: int):
    if getattr(method, "__agentloop_cursor_validation__", False):
        return method
    method_signature = signature(method)

    @wraps(method)
    def wrapper(self, *args, **kwargs):
        bound = method_signature.bind_partial(self, *args, **kwargs)
        cursor = bound.arguments.get("cursor")
        if cursor:
            decode_cursor(cursor, expected_parts=expected_parts)
        return method(self, *args, **kwargs)

    wrapper.__agentloop_cursor_validation__ = True
    return wrapper


def install_store_decoder() -> None:
    """Install strict decoder and endpoint-specific arity validation."""
    import agentloop.store as store

    store.decode_cursor = decode_cursor
    for store_type in (store.SQLiteTraceStore, store.PostgresTraceStore):
        store_type.list_traces_page = _page_validator(store_type.list_traces_page, expected_parts=2)
        store_type.list_findings_page = _page_validator(
            store_type.list_findings_page, expected_parts=3
        )
