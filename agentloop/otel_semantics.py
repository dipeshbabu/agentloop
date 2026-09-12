"""Normalized fields for pinned GenAI, OpenInference, and MCP conventions."""

from __future__ import annotations

import math
from typing import Any

from agentloop.schema import TraceValidationError

INPUT_USAGE = ("gen_ai.usage.input_tokens", "llm.token_count.prompt", "llm.usage.prompt_tokens")
OUTPUT_USAGE = (
    "gen_ai.usage.output_tokens",
    "llm.token_count.completion",
    "llm.usage.completion_tokens",
)
_OI_KINDS = {
    "LLM": "model",
    "EMBEDDING": "model",
    "CHAIN": "workflow",
    "AGENT": "agent",
    "TOOL": "tool",
    "RETRIEVER": "retriever",
    "RERANKER": "reranker",
    "GUARDRAIL": "guardrail",
    "EVALUATOR": "evaluator",
}
_MEMORY_OPERATIONS = {
    "create_memory",
    "create_memory_store",
    "delete_memory",
    "delete_memory_store",
    "search_memory",
    "update_memory",
    "upsert_memory",
}


def extended_kind(attrs: dict[str, Any]) -> str | None:
    operation = attrs.get("gen_ai.operation.name")
    if isinstance(operation, str) and operation in _MEMORY_OPERATIONS:
        return "memory"
    if isinstance(operation, str) and operation in {"plan", "fetch_response"}:
        return "unknown"
    if operation:
        return None
    if "openinference.span.kind" in attrs:
        return _OI_KINDS.get(str(attrs["openinference.span.kind"]).upper(), "unknown")
    if "mcp.method.name" in attrs:
        return {"tools/call": "tool", "resources/read": "retriever"}.get(
            str(attrs["mcp.method.name"]), "unknown"
        )
    if any(key in attrs for key in ("http.request.method", "http.method", "rpc.system")):
        return "unknown"
    return None


def usage_count(attrs: dict[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        if key not in attrs:
            continue
        value = attrs[key]
        if isinstance(value, bool) or not isinstance(value, int | str):
            raise TraceValidationError(key, "usage must be a nonnegative integer")
        try:
            result = int(value)
        except ValueError as exc:
            raise TraceValidationError(key, "usage must be a nonnegative integer") from exc
        if result < 0:
            raise TraceValidationError(key, "usage must be a nonnegative integer")
        return result
    return 0


def normalize_metadata(attrs: dict[str, Any], metadata: dict[str, Any]) -> None:
    aliases = {
        "provider": ("gen_ai.provider.name", "gen_ai.system", "llm.provider"),
        "conversation_id": ("gen_ai.conversation.id",),
        "session_id": ("session.id", "mcp.session.id"),
        "tool_call_id": ("gen_ai.tool.call.id", "tool.id", "tool_call.id"),
        "error_type": ("error.type", "exception.type"),
    }
    for target, keys in aliases.items():
        for key in keys:
            if isinstance(attrs.get(key), str):
                metadata.setdefault(target, attrs[key])
                break
    for target, keys in {
        "cached_input_tokens": (
            "gen_ai.usage.cache_read.input_tokens",
            "llm.token_count.prompt_details.cache_read",
        ),
        "cache_write_input_tokens": (
            "gen_ai.usage.cache_write.input_tokens",
            "llm.token_count.prompt_details.cache_write",
        ),
        "reasoning_output_tokens": (
            "gen_ai.usage.reasoning.output_tokens",
            "llm.token_count.completion_details.reasoning",
        ),
    }.items():
        if any(key in attrs for key in keys):
            metadata.setdefault(target, usage_count(attrs, keys))
    reported_cost = attrs.get("llm.cost.total")
    if (
        not isinstance(reported_cost, bool)
        and isinstance(reported_cost, int | float)
        and math.isfinite(reported_cost)
        and reported_cost >= 0
    ):
        metadata.setdefault("provider_reported_cost_usd", reported_cost)


def evaluation_result(attrs: dict[str, Any], *, source: str) -> dict[str, Any] | None:
    families = (
        (
            "gen_ai.evaluation.name",
            "gen_ai.evaluation.score.value",
            "gen_ai.evaluation.score.label",
            "gen_ai.evaluation.explanation",
        ),
        ("evaluation.name", "evaluation.score", "evaluation.label", "evaluation.explanation"),
        ("annotation.name", "annotation.score", "annotation.label", "annotation.explanation"),
    )
    for name, score, label, explanation in families:
        if name in attrs:
            return {
                "source": source,
                "name": attrs[name],
                "score": attrs.get(score),
                "label": attrs.get(label),
                "explanation": attrs.get(explanation),
                "interpretation": "external evaluation; scale and acceptance criteria are source-defined",
            }
    return None
