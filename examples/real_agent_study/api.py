"""Explicit, bounded local inference with native 0.7 model-call evidence."""

from __future__ import annotations

import json
import time
import urllib.request
from urllib.parse import urlsplit

from agentloop.events import utc_now_iso
from agentloop.tracer import record_model_call


class LocalModel:
    def __init__(self, base_url, identity, *, max_tokens, timeout_s, seed, trace=None):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("study inference must use an explicit loopback HTTP endpoint")
        if type(max_tokens) is not int or not 1 <= max_tokens <= 512 or not 0 < timeout_s <= 180:
            raise ValueError("invalid inference bounds")
        self.url = base_url.rstrip("/") + "/v1/chat/completions"
        self.identity = dict(identity)
        self.max_tokens, self.timeout_s, self.seed, self.trace = max_tokens, timeout_s, seed, trace
        self.receipts = []
        self.recording_ms = 0.0
        self.response_schema = None

    def complete(self, messages, *, remaining_s):
        payload = {
            "model": "local-study",
            "messages": messages,
            "temperature": 0,
            "seed": self.seed,
            "max_tokens": self.max_tokens,
            "cache_prompt": False,
            "response_format": {"type": "json_object"},
        }
        if self.response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "study_decision",
                    "strict": True,
                    "schema": self.response_schema,
                },
            }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > 40000 or remaining_s <= 0:
            raise ValueError("request exceeds the frozen input or time bound")
        request = urllib.request.Request(
            self.url, data=encoded, headers={"Content-Type": "application/json"}
        )
        started, start = utc_now_iso(), time.perf_counter()
        usage, content, status, error, finish_reason, timings = {}, None, "ok", None, None, None
        try:
            with urllib.request.urlopen(
                request, timeout=min(self.timeout_s, remaining_s)
            ) as response:  # nosec B310 - loopback URL validated above
                raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError("inference response exceeds 1 MB")
            result = json.loads(raw)
            usage = result.get("usage")
            if not isinstance(usage, dict):
                usage = {}
            content = result["choices"][0]["message"]["content"]
            finish_reason = result["choices"][0].get("finish_reason")
            timings = result.get("timings")
            if not isinstance(content, str):
                raise ValueError("missing model text")
        except Exception as exc:
            status, error = "error", type(exc).__name__
            raise
        finally:
            duration = (time.perf_counter() - start) * 1000
            ended = utc_now_iso()
            known = all(
                type(usage.get(key)) is int and usage[key] >= 0
                for key in ("prompt_tokens", "completion_tokens")
            )
            receipt = {
                "call": len(self.receipts),
                "started_at": started,
                "ended_at": ended,
                "duration_ms": duration,
                "status": status,
                "error_category": error,
                "model": self.identity,
                "usage": usage if known else None,
                "finish_reason": finish_reason,
                "timings": timings,
                "output_text": content,
            }
            self.receipts.append(receipt)
            if self.trace is not None:
                record_start = time.perf_counter()
                record_model_call(
                    "agent_decision",
                    trace=self.trace,
                    duration_ms=duration,
                    started_at=started,
                    ended_at=ended,
                    model=self.identity["id"],
                    input_tokens=usage["prompt_tokens"] if known else 0,
                    output_tokens=usage["completion_tokens"] if known else 0,
                    token_provenance="provider" if known else "unavailable",
                    input_text=json.dumps(messages, ensure_ascii=False),
                    output_text=content,
                    status=status,
                    error=error,
                    metadata={
                        "model_identity": self.identity,
                        "provider": "local-llama.cpp",
                        "usage_source": "local_server_response" if known else "unavailable",
                        "paid_provider_spend_usd": 0,
                        "operating_cost_status": "unavailable",
                        "cache_prompt": False,
                        "seed": self.seed,
                        "finish_reason": finish_reason,
                    },
                )
                self.recording_ms += (time.perf_counter() - record_start) * 1000
        return content
