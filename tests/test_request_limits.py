from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from agentloop.request_limits import (
    DEFAULT_MAX_REQUEST_BYTES,
    RequestSizeLimitMiddleware,
    max_request_bytes,
)


class Payload(BaseModel):
    text: str


def _client(limit):
    app = FastAPI()
    app.add_middleware(RequestSizeLimitMiddleware, limit=limit)

    @app.post("/echo")
    def echo(payload: Payload) -> dict[str, int]:
        return {"length": len(payload.text)}

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"status": "ok"}

    return TestClient(app)


def test_requests_within_the_limit_pass():
    client = _client(100)
    assert client.post("/echo", json={"text": "a" * 50}).json() == {"length": 50}
    assert client.get("/ping").status_code == 200


def test_a_declared_oversized_body_is_refused_before_reading():
    response = _client(100).post("/echo", json={"text": "a" * 200})
    assert response.status_code == 413
    assert "100-byte limit" in response.json()["detail"]


def test_a_streamed_body_without_a_length_is_counted():
    def chunks():
        yield b'{"text": "'
        for _ in range(20):
            yield b"a" * 10
        yield b'"}'

    response = _client(100).post(
        "/echo", content=chunks(), headers={"content-type": "application/json"}
    )
    assert response.status_code == 413


def test_zero_disables_the_limit():
    assert _client(None).post("/echo", json={"text": "a" * 5000}).json() == {"length": 5000}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, DEFAULT_MAX_REQUEST_BYTES),
        ("", DEFAULT_MAX_REQUEST_BYTES),
        ("0", None),
        ("2048", 2048),
    ],
)
def test_limit_setting(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("AGENTLOOP_MAX_REQUEST_BYTES", raising=False)
    else:
        monkeypatch.setenv("AGENTLOOP_MAX_REQUEST_BYTES", value)
    assert max_request_bytes() == expected


@pytest.mark.parametrize("value", ["-1", "16MB", "1.5"])
def test_invalid_limit_settings_fail_loudly(value):
    with pytest.raises(ValueError, match="AGENTLOOP_MAX_REQUEST_BYTES"):
        max_request_bytes(value)


def test_the_api_server_refuses_uploads_over_the_default_limit():
    from agentloop.server import app

    oversized = {"name": "x", "run_id": "too-big", "metadata": {"blob": "a" * (17 * 1024 * 1024)}}
    response = TestClient(app).post("/v1/traces", json=oversized)
    assert response.status_code == 413
