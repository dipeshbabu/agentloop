"""HTTP hardening for the AgentLoop API when no proxy adds it.

Responses can contain prompts, tool inputs and outputs, so every API response
asks clients and intermediaries not to store it or sniff its type. The
interactive schema pages can be switched off for internet-facing servers.
"""

from __future__ import annotations

import os

API_DOCS_VARIABLE = "AGENTLOOP_API_DOCS"
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
)


def api_docs_enabled(value: str | None = None) -> bool:
    """Whether /docs, /redoc and /openapi.json are served (default: yes)."""
    text = os.getenv(API_DOCS_VARIABLE) if value is None else value
    if text is None or not text.strip():
        return True
    normalized = text.strip().lower()
    if normalized in _TRUE:
        return True
    if normalized in _FALSE:
        return False
    raise ValueError(f"{API_DOCS_VARIABLE} must be true or false")


class SecurityHeadersMiddleware:
    """ASGI middleware adding defensive headers unless a response sets its own."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers.extend(
                    (name, value) for name, value in SECURITY_HEADERS if name not in present
                )
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
