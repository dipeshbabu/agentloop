from __future__ import annotations

from fastapi.testclient import TestClient

from agentloop.server import app


def test_malformed_but_decodable_trace_cursor_returns_400(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(tmp_path / "cursor-api.db"))
    client = TestClient(app)

    response = client.get("/traces?page_size=1&cursor=W10")

    assert response.status_code == 400
    assert response.json()["detail"].startswith("invalid pagination cursor")


def test_malformed_but_decodable_finding_cursor_returns_400(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(tmp_path / "cursor-api.db"))
    client = TestClient(app)

    response = client.get("/findings?page_size=1&cursor=W10")

    assert response.status_code == 400
    assert response.json()["detail"].startswith("invalid pagination cursor")
