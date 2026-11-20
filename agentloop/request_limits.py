"""Reject oversized API requests before they are buffered.

Deployments without a reverse proxy (managed container platforms, a direct
``agentloop server``) would otherwise read request bodies of any size into
memory. The limit applies to the declared ``Content-Length`` and, for
streamed or chunked bodies, to the bytes actually received.
"""

from __future__ import annotations

import json
import os

DEFAULT_MAX_REQUEST_BYTES = 16 * 1024 * 1024
ENVIRONMENT_VARIABLE = "AGENTLOOP_MAX_REQUEST_BYTES"


def max_request_bytes(value: str | None = None) -> int | None:
    """Parse the limit; ``0`` disables it. Invalid values fail at startup."""
    text = os.getenv(ENVIRONMENT_VARIABLE) if value is None else value
    if text is None or not text.strip():
        return DEFAULT_MAX_REQUEST_BYTES
    try:
        limit = int(text.strip())
    except ValueError:
        raise ValueError(f"{ENVIRONMENT_VARIABLE} must be a whole number of bytes") from None
    if limit < 0:
        raise ValueError(f"{ENVIRONMENT_VARIABLE} must be 0 (no limit) or a positive size")
    return limit or None


class RequestSizeLimitMiddleware:
    """ASGI middleware answering 413 for request bodies over ``limit`` bytes."""

    def __init__(self, app, limit: int | None) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or self.limit is None:
            await self.app(scope, receive, send)
            return
        limit = self.limit
        declared = None
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = None
        if declared is not None:
            # The HTTP server already refuses bodies longer than their declared length.
            if declared > limit:
                await _reject(send, limit)
                return
            await self.app(scope, receive, send)
            return

        # No declared length (for example chunked uploads): read up to the limit
        # here, so the application never buffers more, then replay the body.
        chunks: list[bytes] = []
        received = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                # The client disconnected; let the application see that.
                replay = [message]
                break
            body = message.get("body", b"")
            received += len(body)
            if received > limit:
                await _reject(send, limit)
                return
            chunks.append(body)
            if not message.get("more_body", False):
                replay = [{"type": "http.request", "body": b"".join(chunks), "more_body": False}]
                break

        async def replay_receive():
            if replay:
                return replay.pop(0)
            return await receive()

        await self.app(scope, replay_receive, send)


async def _reject(send, limit: int) -> None:
    body = json.dumps(
        {"detail": f"request body exceeds the {limit}-byte limit ({ENVIRONMENT_VARIABLE})"}
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
