from __future__ import annotations

import base64
import json

import pytest

from agentloop.store import InvalidCursorError, PostgresTraceStore, SQLiteTraceStore, decode_cursor


def _cursor(value) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@pytest.mark.parametrize(
    "payload",
    [
        [],
        ["only-one"],
        ["a", "b", "c", "d"],
        [None, "run"],
        ["created", None],
        [123, "run"],
        ["created", {"run": "nested"}],
        ["updated", "run", ["finding"]],
        ["", "run"],
    ],
)
def test_decode_cursor_rejects_malformed_but_decodable_shapes(payload) -> None:
    with pytest.raises(InvalidCursorError, match="invalid pagination cursor"):
        decode_cursor(_cursor(payload))


@pytest.mark.parametrize(
    "payload",
    [
        ["2026-01-01T00:00:00+00:00", "run-1"],
        ["2026-01-01T00:00:00+00:00", "run-1", "finding-1"],
    ],
)
def test_decode_cursor_accepts_supported_shapes(payload) -> None:
    assert decode_cursor(_cursor(payload)) == payload


@pytest.mark.parametrize("kind", ["traces", "findings"])
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_store_backends_reject_wrong_cursor_arity_before_query(tmp_path, kind: str, backend: str) -> None:
    if backend == "sqlite":
        store = SQLiteTraceStore(path=str(tmp_path / "cursor.db"))
    else:
        # Cursor validation runs before any connection is opened, so this DSN
        # deliberately does not need a live Postgres server.
        store = PostgresTraceStore(dsn="postgresql://invalid:invalid@127.0.0.1:1/invalid")

    cursor = _cursor([])
    method = store.list_traces_page if kind == "traces" else store.list_findings_page

    with pytest.raises(InvalidCursorError, match="invalid pagination cursor"):
        method(limit=1, cursor=cursor)
