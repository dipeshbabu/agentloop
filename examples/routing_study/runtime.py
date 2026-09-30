"""Pinned local tokenization and inference; never a remote paid provider."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from agentloop.budget_types import DispatchOptions, Reservation, ResourceUsage
from agentloop.context_types import ContextTokenCount
from agentloop.events import utc_now_iso
from agentloop.routing_types import (
    ModelBackend,
    ModelCapabilities,
    ModelIdentity,
    RouteProviderError,
    RouteResponseInfo,
)
from agentloop.tracer import record_model_call
from examples.real_agent_study.run import MODELS, fingerprint


def identity(condition):
    return ModelIdentity("llama.cpp", MODELS[condition]["id"])


def inspect_reply(reply):
    raw = reply.get("usage")
    known = isinstance(raw, dict) and all(
        type(raw.get(key)) is int and raw[key] >= 0
        for key in ("prompt_tokens", "completion_tokens")
    )
    usage = (
        ResourceUsage(
            tokens=raw["prompt_tokens"] + raw["completion_tokens"],
            token_provenance="provider",
            complete=True,
        )
        if known
        else None
    )
    model = reply.get("model")
    # The server may report a filesystem path as its model alias. Retain its
    # basename, while separate server/file SHA verification pins the artifact.
    model = (
        model.replace("\\", "/").rsplit("/", 1)[-1] if isinstance(model, str) and model else None
    )
    return RouteResponseInfo(usage, model)


class LocalBackend:
    def __init__(self, base_url, condition, trace=None):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("only an explicit loopback inference server is supported")
        self.base_url, self.condition, self.trace = base_url.rstrip("/"), condition, trace
        self.calls, self.tokenization = [], []

    def _post(self, endpoint, payload):
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(data) > 40000:
            raise ValueError("request exceeds study bound")
        request = urllib.request.Request(
            self.base_url + endpoint, data=data, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=90) as response:  # nosec B310 - loopback endpoint validated
            raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError("response exceeds study bound")
        return json.loads(raw)

    def count(self, payload):
        if payload["model"] != identity(self.condition).model:
            return ContextTokenCount()
        start = time.perf_counter()
        prompt = self._post("/apply-template", {"messages": payload["messages"]})["prompt"]
        tokens = self._post(
            "/tokenize", {"content": prompt, "add_special": False, "parse_special": True}
        )["tokens"]
        if not isinstance(tokens, list) or any(type(token) is not int for token in tokens):
            raise ValueError("invalid tokenization response")
        count = ContextTokenCount(
            len(tokens), "tokenizer", "gguf-" + MODELS[self.condition]["sha256"]
        )
        self.tokenization.append(
            {
                "request_hash": fingerprint(payload),
                "count": count.to_dict(),
                "latency_ms": (time.perf_counter() - start) * 1000,
            }
        )
        return count

    def invoke(self, payload):
        if payload["model"] != identity(self.condition).model:
            raise ValueError("requested backend is not the verified loaded model")
        started, start = utc_now_iso(), time.perf_counter()
        reply, content, error, info, count_matches = None, None, None, RouteResponseInfo(), None
        try:
            reply = self._post("/v1/chat/completions", payload)
            info = inspect_reply(reply)
            if self.tokenization and info.usage is not None:
                counted = self.tokenization[-1]
                if counted["request_hash"] != fingerprint(payload):
                    raise ValueError("tokenization does not match this request")
                count_matches = counted["count"]["value"] == reply["usage"]["prompt_tokens"]
                if not count_matches:
                    raise ValueError("pre-dispatch token count differs from provider usage")
            content = reply["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("missing model text")
            return reply
        except BaseException as exc:
            if not isinstance(exc, Exception):
                error = "cancelled"
                raise
            error = (
                "rate_limit"
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 429
                else "unavailable"
                if isinstance(exc, urllib.error.HTTPError) and exc.code in {502, 503, 504}
                else "timeout"
                if isinstance(exc, TimeoutError)
                else "provider_error"
            )
            raise RouteProviderError(error, info=info) from exc
        finally:
            elapsed, ended = (time.perf_counter() - start) * 1000, utc_now_iso()
            raw_usage = (
                reply.get("usage") if isinstance(reply, dict) and info.usage is not None else None
            )
            self.calls.append(
                {
                    "model": MODELS[self.condition],
                    "provider_reported_model": info.reported_model,
                    "status": "failed" if error else "completed",
                    "error_category": error,
                    "usage": raw_usage,
                    "output_text": content,
                    "started_at": started,
                    "ended_at": ended,
                    "latency_ms": elapsed,
                    "token_count_matches_provider": count_matches,
                }
            )
            if self.trace is not None:
                record_model_call(
                    "routed_answer",
                    trace=self.trace,
                    started_at=started,
                    ended_at=ended,
                    duration_ms=elapsed,
                    model=identity(self.condition).model,
                    input_tokens=raw_usage["prompt_tokens"] if raw_usage else 0,
                    output_tokens=raw_usage["completion_tokens"] if raw_usage else 0,
                    token_provenance="provider" if raw_usage else "unavailable",
                    input_text=json.dumps(payload["messages"], ensure_ascii=False),
                    output_text=content,
                    status="error" if error else "ok",
                    error=error,
                    metadata={
                        "model_sha256": MODELS[self.condition]["sha256"],
                        "provider_reported_model": info.reported_model,
                        "paid_provider_spend_usd": 0,
                        "operating_cost_status": "unknown",
                    },
                )

    def backend(self, condition):
        return ModelBackend(
            identity(condition),
            ModelCapabilities(
                "llama-b10964-text-json-v1",
                4096,
                parameters=(
                    "model",
                    "messages",
                    "max_tokens",
                    "temperature",
                    "seed",
                    "cache_prompt",
                    "response_format",
                ),
                output_modes=("text", "json_object"),
            ),
            self.invoke,
            self.count,
            inspect_reply,
            dispatch=DispatchOptions(Reservation(tokens=4160, provenance="upper_bound")),
        )
