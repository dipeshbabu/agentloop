from __future__ import annotations

import base64
import json

from fastapi.testclient import TestClient

from agentloop.server import app


def _cursor(parts: list[str]) -> str:
    raw = json.dumps(parts, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def test_malformed_but_decodable_trace_cursor_returns_400(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(tmp_path / "cursor-api.db"))
    client = TestClient(app)

    response = client.get("/traces?page_size=1&cursor=W10")

    assert response.status_code == 400
    assert response.json()["detail"].startswith("invalid pagination cursor")


def test_finding_cursor_is_rejected_by_trace_endpoint(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(tmp_path / "cursor-api.db"))
    client = TestClient(app)

    response = client.get(
        "/traces",
        params={"page_size": 1, "cursor": _cursor(["updated", "run-1", "finding-1"])},
    )

    assert response.status_code == 400
    assert response.json()["detail"].startswith("invalid pagination cursor")


def test_malformed_but_decodable_finding_cursor_returns_400(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(tmp_path / "cursor-api.db"))
    client = TestClient(app)

    response = client.get("/findings?page_size=1&cursor=W10")

    assert response.status_code == 400
    assert response.json()["detail"].startswith("invalid pagination cursor")


def test_trace_cursor_is_rejected_by_finding_endpoint(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(tmp_path / "cursor-api.db"))
    client = TestClient(app)

    response = client.get(
        "/findings",
        params={"page_size": 1, "cursor": _cursor(["created", "run-1"])},
    )

    assert response.status_code == 400
    assert response.json()["detail"].startswith("invalid pagination cursor")
