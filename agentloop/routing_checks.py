"""Conservative checks before an explicitly configured model substitution."""

from __future__ import annotations

from copy import deepcopy

from agentloop.context_types import EXACT_COUNTS, ContextTokenCount
from agentloop.harness import _invoke_usage_reader


def check_route(backend, payload, *, streaming):
    capabilities = backend.capabilities
    reasons = []
    if capabilities.adapter != "json_chat_v1":
        reasons.append("unsupported_adapter")
    if not set(payload) <= set(capabilities.parameters):
        reasons.append("unsupported_parameters")
    if streaming and (not capabilities.streaming or backend.stream is None):
        reasons.append("streaming_unsupported")
    if payload.get("stream", False) is not streaming:
        reasons.append("stream_mode_mismatch")
    messages = payload.get("messages")
    modalities, has_tools = set(), bool(payload.get("tools"))
    if not isinstance(messages, list) or not messages or len(messages) > 512:
        reasons.append("invalid_messages")
        messages = []
    for message in messages:
        if (
            not isinstance(message, dict)
            or not isinstance(message.get("role"), str)
            or message.get("role")
            not in {
                "system",
                "developer",
                "user",
                "assistant",
                "tool",
            }
        ):
            reasons.append("unsupported_message")
            continue
        has_tools = has_tools or message["role"] == "tool" or bool(message.get("tool_calls"))
        content = message.get("content")
        if isinstance(content, str):
            modalities.add("text")
        elif content is None and message["role"] == "assistant" and message.get("tool_calls"):
            modalities.add("text")
        elif isinstance(content, list) and content:
            for part in content:
                kind = part.get("type") if isinstance(part, dict) else None
                mapped = (
                    {"text": "text", "image_url": "image", "input_audio": "audio"}.get(kind)
                    if isinstance(kind, str)
                    else None
                )
                if mapped is None:
                    reasons.append("unsupported_content")
                else:
                    modalities.add(mapped)
        else:
            reasons.append("unsupported_content")
    if not modalities <= set(capabilities.modalities):
        reasons.append("modality_unsupported")
    if has_tools and not capabilities.tool_calling:
        reasons.append("tools_unsupported")
    if payload.get("tools") is not None and (
        not isinstance(payload["tools"], list)
        or not payload["tools"]
        or any(
            not isinstance(tool, dict)
            or tool.get("type") != "function"
            or not isinstance(tool.get("function"), dict)
            for tool in payload["tools"]
        )
    ):
        reasons.append("unsupported_tool_format")
    response = payload.get("response_format", {"type": "text"})
    mode = response.get("type") if isinstance(response, dict) else None
    if not isinstance(mode, str) or mode not in capabilities.output_modes:
        reasons.append("output_mode_unsupported")
    if mode == "json_schema" and (
        not isinstance(response.get("json_schema"), dict)
        or not isinstance(response["json_schema"].get("schema"), dict)
    ):
        reasons.append("invalid_output_schema")
    bounds = [payload[key] for key in ("max_tokens", "max_completion_tokens") if key in payload]
    output_bound = (
        bounds[0] if len(bounds) == 1 and type(bounds[0]) is int and bounds[0] > 0 else None
    )
    if output_bound is None:
        reasons.append("output_bound_unavailable")
    count = ContextTokenCount()
    count_attempted = not reasons
    if count_attempted:
        try:
            value = _invoke_usage_reader(backend.token_counter, deepcopy(payload))
            if type(value) is ContextTokenCount:
                count = value
            else:
                reasons.append("invalid_token_count")
        except Exception:
            reasons.append("token_counter_failed")
        if count.provenance not in EXACT_COUNTS or count.value is None:
            reasons.append("context_count_unverified")
        elif output_bound is not None and count.value + output_bound > capabilities.context_window:
            reasons.append("context_limit")
    return {
        "supported": not reasons,
        "reasons": sorted(set(reasons)),
        "capability_reference": capabilities.reference,
        "adapter": capabilities.adapter,
        "input_tokens": count.to_dict(),
        "token_count_attempted": count_attempted,
        "output_token_bound": output_bound,
        "context_window": capabilities.context_window,
    }
