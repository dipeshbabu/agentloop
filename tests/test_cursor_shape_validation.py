from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

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


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("traces", []),
        ("traces", ["updated", "run-1", "finding-1"]),
        ("findings", []),
        ("findings", ["created", "run-1"]),
    ],
)
def test_store_backends_reject_wrong_cursor_arity_before_query(
    tmp_path, kind: str, payload, backend: str
) -> None:
    if backend == "sqlite":
        store = SQLiteTraceStore(path=str(tmp_path / "cursor.db"))
    else:
        # Cursor validation runs before any connection is opened, so this DSN
        # deliberately does not need a live Postgres server.
        store = PostgresTraceStore(dsn="postgresql://invalid:invalid@127.0.0.1:1/invalid")

    cursor = _cursor(payload)
    method = store.list_traces_page if kind == "traces" else store.list_findings_page

    with pytest.raises(InvalidCursorError, match="invalid pagination cursor"):
        method(limit=1, cursor=cursor)


@pytest.mark.parametrize("import_first", ["agentloop", "agentloop.store"])
def test_store_cursor_validation_survives_module_reload(import_first: str) -> None:
    # Re-importing storage must not depend on an earlier package initializer
    # mutating its functions or classes. Keep reload isolated from other tests.
    script = f"""
import importlib
importlib.import_module({import_first!r})
store = importlib.reload(importlib.import_module('agentloop.store'))
for backend in (store.SQLiteTraceStore, store.PostgresTraceStore):
    instance = backend()
    for method, parts in (
        (instance.list_traces_page, ['updated', 'run', 'finding']),
        (instance.list_findings_page, ['created', 'run']),
    ):
        try:
            method(cursor=store.encode_cursor(parts))
        except store.InvalidCursorError:
            pass
        else:
            raise AssertionError('cross-endpoint cursor accepted')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
