from __future__ import annotations

import os
import subprocess
import sys

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from agentloop.http_security import SecurityHeadersMiddleware, api_docs_enabled


def test_responses_carry_security_headers_without_overriding_route_choices():
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/plain")
    def plain() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/cached")
    def cached() -> JSONResponse:
        return JSONResponse({"ok": "yes"}, headers={"Cache-Control": "max-age=60"})

    client = TestClient(app)
    plain_headers = client.get("/plain").headers
    assert plain_headers["x-content-type-options"] == "nosniff"
    assert plain_headers["referrer-policy"] == "no-referrer"
    assert plain_headers["cache-control"] == "no-store"
    assert client.get("/cached").headers["cache-control"] == "max-age=60"
    assert client.get("/missing").headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, True), ("", True), ("true", True), ("ON", True), ("false", False), ("0", False)],
)
def test_api_docs_setting(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("AGENTLOOP_API_DOCS", raising=False)
    else:
        monkeypatch.setenv("AGENTLOOP_API_DOCS", value)
    assert api_docs_enabled() is expected


def test_invalid_api_docs_setting_fails_loudly():
    with pytest.raises(ValueError, match="AGENTLOOP_API_DOCS"):
        api_docs_enabled("sometimes")


def test_the_api_server_sends_security_headers():
    from agentloop.server import app

    headers = TestClient(app).get("/v1/health").headers
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(("setting", "status"), [("false", 404), ("true", 200)])
def test_api_docs_can_be_switched_off(setting, status):
    script = (
        "from fastapi.testclient import TestClient\n"
        "from agentloop.server import app\n"
        "client = TestClient(app)\n"
        "print(client.get('/docs').status_code, client.get('/openapi.json').status_code)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "AGENTLOOP_API_DOCS": setting},
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert result.stdout.split() == [str(status), str(status)]
