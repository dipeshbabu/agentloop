"""A loopback-only chat client that records every call into the active trace."""

from __future__ import annotations

import json
import time
import urllib.request
from urllib.parse import urlsplit

from agentloop.events import utc_now_iso
from agentloop.tokens import PROVIDER
from agentloop.tracer import record_model_call


class LocalModel:
    """One llama.cpp server on 127.0.0.1; deterministic decoding."""

    def __init__(self, base_url: str, name: str, *, timeout_s: float = 300.0) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("benchmark inference must use a loopback HTTP endpoint")
        self.url = base_url.rstrip("/") + "/v1/chat/completions"
        self.name = name
        self.timeout_s = timeout_s

    def chat(
        self, step: str, messages: list[dict], *, max_tokens: int, json_mode: bool = False
    ) -> str:
        payload = {
            "messages": messages,
            "temperature": 0,
            "seed": 0,
            "max_tokens": max_tokens,
            "cache_prompt": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=body, headers={"Content-Type": "application/json"}
        )
        started = utc_now_iso()
        clock = time.perf_counter()
        status, error, text, usage = "ok", None, "", {}
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:  # nosec B310 - loopback only
                data = json.load(response)
            text = data["choices"][0]["message"]["content"] or ""
            usage = data.get("usage") or {}
        except Exception as exc:  # noqa: BLE001 - recorded, then raised
            status, error = "error", f"{type(exc).__name__}: {exc}"[:300]
            raise
        finally:
            record_model_call(
                step,
                duration_ms=(time.perf_counter() - clock) * 1000,
                started_at=started,
                model=self.name,
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
                token_provenance=PROVIDER if usage else None,
                status=status,
                error=error,
                metadata={"agentloop.effects": {"effect": "none", "deterministic": True}},
            )
        return text
