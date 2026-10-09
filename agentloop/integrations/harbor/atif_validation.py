"""Narrow strict validation against the pinned ATIF v1.6/v1.7/v1.8 contracts."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from agentloop.interoperability.validation import ImportLimits, ImportValidationError

SUPPORTED_ATIF = frozenset({"ATIF-v1.6", "ATIF-v1.7", "ATIF-v1.8"})


def fail(field: str, reason: str, code: str = "invalid_atif") -> None:
    raise ImportValidationError(code, field, reason)


def obj(value: Any, field: str, allowed: set[str], required: set[str] = frozenset()) -> dict:
    if not isinstance(value, dict) or set(value) - allowed or required - set(value):
        fail(field, "invalid object fields")
    return value


def label(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 512:
        fail(field, "identifier must be a nonempty bounded string")
    return value


def sequence(value: Any, field: str) -> list:
    if not isinstance(value, list):
        fail(field, "must be an array")
    return value


def integer(value: Any, field: str, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        fail(field, "must be a nonnegative integer")


def number(value: Any, field: str, *, nonnegative: bool = True) -> None:
    if type(value) not in {int, float} or not math.isfinite(value) or nonnegative and value < 0:
        fail(field, "must be a finite number in the supported range")


def timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        fail(field, "timestamp must be an ISO-8601 string")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ImportValidationError("invalid_atif", field, "invalid ISO-8601 timestamp") from None


def validate_extra(value: Any, field: str) -> None:
    if value is not None and not isinstance(value, dict):
        fail(field, "extra must be an object or null")


def metrics(value: Any, field: str, *, final: bool = False) -> None:
    prefix = "total_" if final else ""
    token_fields = {prefix + key for key in ("prompt_tokens", "completion_tokens", "cached_tokens")}
    allowed = {*token_fields, prefix + "cost_usd", "extra"}
    allowed |= (
        {"total_steps"} if final else {"prompt_token_ids", "completion_token_ids", "logprobs"}
    )
    obj(value, field, allowed)
    for key in token_fields | ({"total_steps"} if final else set()):
        if value.get(key) is not None:
            integer(value[key], field + "." + key)
    if value.get(prefix + "cost_usd") is not None:
        number(value[prefix + "cost_usd"], field + "." + prefix + "cost_usd")
    prompt, cache = value.get(prefix + "prompt_tokens"), value.get(prefix + "cached_tokens")
    if prompt is not None and cache is not None and cache > prompt:
        fail(field, "cached tokens must be a subset of prompt tokens")
    if not final:
        for key in ("prompt_token_ids", "completion_token_ids"):
            if value.get(key) is not None:
                for item in sequence(value[key], field + "." + key):
                    integer(item, field + "." + key)
        if value.get("logprobs") is not None:
            for item in sequence(value["logprobs"], field + ".logprobs"):
                number(item, field + ".logprobs", nonnegative=False)
    validate_extra(value.get("extra"), field + ".extra")


def content(value: Any, field: str, version: str, *, nullable: bool = False) -> None:
    if value is None and nullable or isinstance(value, str):
        return
    for part in sequence(value, field):
        obj(part, field, {"type", "text", "source"}, {"type"})
        kind = part["type"]
        if not isinstance(kind, str) or kind not in {"text", "image", "audio"}:
            fail(field, "unsupported content part")
        if kind == "text":
            if not isinstance(part.get("text"), str) or part.get("source") is not None:
                fail(field, "invalid text part")
            continue
        if kind == "audio" and version != "ATIF-v1.8":
            fail(field, "audio requires ATIF v1.8", "unsupported_atif_semantics")
        source = obj(
            part.get("source"),
            field + ".source",
            {"media_type", "path", "duration_sec"} if kind == "audio" else {"media_type", "path"},
            {"media_type", "path"},
        )
        if (
            part.get("text") is not None
            or not isinstance(source["path"], str)
            or not source["path"]
        ):
            fail(field, "invalid media part")
        media = source["media_type"]
        if not isinstance(media, str):
            fail(field, "media type must be a string")
        allowed = (
            {"image/jpeg", "image/png", "image/gif", "image/webp"}
            if kind == "image"
            else {
                "audio/wav",
                "audio/mpeg",
                "audio/mp4",
                "audio/aac",
                "audio/ogg",
                "audio/flac",
                "audio/webm",
                "audio/aiff",
                "audio/mp3",
                "audio/mpga",
                "audio/x-mpeg",
                "audio/x-wav",
                "audio/wave",
                "audio/vnd.wave",
                "audio/x-m4a",
                "audio/m4a",
                "audio/x-aac",
                "audio/x-flac",
                "audio/x-aiff",
            }
        )
        if media.strip().lower() not in allowed:
            fail(field, "unsupported media type")
        if source.get("duration_sec") is not None:
            number(source["duration_sec"], field + ".source.duration_sec")


def validate_trajectory(payload: Any, limits: ImportLimits) -> dict:
    obj(
        payload,
        "trajectory",
        {
            "schema_version",
            "session_id",
            "trajectory_id",
            "agent",
            "steps",
            "notes",
            "final_metrics",
            "continued_trajectory_ref",
            "extra",
            "subagent_trajectories",
        },
        {"schema_version", "agent", "steps"},
    )
    version = payload["schema_version"]
    if not isinstance(version, str) or version not in SUPPORTED_ATIF:
        fail("schema_version", "unsupported ATIF version", "unsupported_atif_version")
    for key in ("session_id", "trajectory_id", "continued_trajectory_ref"):
        if payload.get(key) is not None:
            label(payload[key], key)
    if payload.get("notes") is not None and not isinstance(payload["notes"], str):
        fail("notes", "notes must be text")
    agent = obj(
        payload["agent"],
        "agent",
        {"name", "version", "model_name", "tool_definitions", "extra"},
        {"name", "version"},
    )
    for key in ("name", "version"):
        label(agent[key], "agent." + key)
    if agent.get("model_name") is not None:
        label(agent["model_name"], "agent.model_name")
    if agent.get("tool_definitions") is not None:
        if any(
            not isinstance(item, dict)
            for item in sequence(agent["tool_definitions"], "agent.tool_definitions")
        ):
            fail("agent.tool_definitions", "tool definitions must be objects")
    validate_extra(agent.get("extra"), "agent.extra")
    validate_extra(payload.get("extra"), "extra")
    if payload.get("final_metrics") is not None:
        metrics(payload["final_metrics"], "final_metrics", final=True)
    steps = sequence(payload["steps"], "steps")
    if not steps or len(steps) > limits.max_events_per_trace:
        fail("steps", "step count outside configured bounds", "limit_exceeded")
    for index, step in enumerate(steps, 1):
        field = f"steps[{index - 1}]"
        obj(
            step,
            field,
            {
                "step_id",
                "timestamp",
                "source",
                "model_name",
                "reasoning_effort",
                "message",
                "reasoning_content",
                "tool_calls",
                "observation",
                "metrics",
                "is_copied_context",
                "llm_call_count",
                "extra",
            },
            {"step_id", "source", "message"},
        )
        if type(step["step_id"]) is not int or step["step_id"] != index:
            fail(field + ".step_id", "step IDs must be sequential starting at one")
        source = step["source"]
        if not isinstance(source, str) or source not in {"agent", "user", "system"}:
            fail(field + ".source", "unsupported step source")
        if step.get("timestamp") is not None:
            timestamp(step["timestamp"], field + ".timestamp")
        content(step["message"], field + ".message", version)
        validate_extra(step.get("extra"), field + ".extra")
        if (
            step.get("is_copied_context") is not None
            and type(step["is_copied_context"]) is not bool
        ):
            fail(field + ".is_copied_context", "copied context flag must be boolean")
        count = step.get("llm_call_count")
        if count is not None:
            integer(count, field + ".llm_call_count")
        agent_fields = (
            "model_name",
            "reasoning_effort",
            "reasoning_content",
            "tool_calls",
            "metrics",
        )
        if source != "agent" and any(step.get(key) is not None for key in agent_fields):
            fail(field, "agent-only fields are present on a non-agent step")
        if (
            count == 0
            and source == "agent"
            and (step.get("metrics") is not None or step.get("reasoning_content") is not None)
        ):
            fail(field, "deterministic dispatch cannot carry model metrics or reasoning")
        if step.get("model_name") is not None:
            label(step["model_name"], field + ".model_name")
        if step.get("reasoning_content") is not None and not isinstance(
            step["reasoning_content"], str
        ):
            fail(field, "reasoning must be text")
        if step.get("reasoning_effort") is not None and type(step["reasoning_effort"]) not in {
            str,
            int,
            float,
        }:
            fail(field, "invalid reasoning effort")
        if step.get("metrics") is not None:
            metrics(step["metrics"], field + ".metrics")
        calls = step.get("tool_calls") or []
        if step.get("tool_calls") is not None:
            sequence(step["tool_calls"], field + ".tool_calls")
        seen = set()
        for call in calls:
            obj(
                call,
                field + ".tool_calls",
                {"tool_call_id", "function_name", "arguments", "extra"},
                {"tool_call_id", "function_name", "arguments"},
            )
            call_id = label(call["tool_call_id"], field + ".tool_call_id")
            label(call["function_name"], field + ".function_name")
            if call_id in seen or not isinstance(call["arguments"], dict):
                fail(field + ".tool_calls", "duplicate tool call identity or invalid arguments")
            seen.add(call_id)
            validate_extra(call.get("extra"), field + ".tool_calls.extra")
        if step.get("observation") is not None:
            observation = obj(step["observation"], field + ".observation", {"results"}, {"results"})
            for result in sequence(observation["results"], field + ".observation.results"):
                obj(
                    result,
                    field + ".observation.results",
                    {"source_call_id", "content", "subagent_trajectory_ref", "extra"},
                )
                if (
                    result.get("source_call_id") is not None
                    and label(result["source_call_id"], field + ".source_call_id") not in seen
                ):
                    fail(field + ".source_call_id", "observation references an unknown tool call")
                content(
                    result.get("content"), field + ".observation.content", version, nullable=True
                )
                validate_extra(result.get("extra"), field + ".observation.extra")
                if result.get("subagent_trajectory_ref") is not None:
                    for reference in sequence(
                        result["subagent_trajectory_ref"], field + ".subagent_trajectory_ref"
                    ):
                        obj(
                            reference,
                            field + ".subagent_trajectory_ref",
                            {"trajectory_id", "session_id", "trajectory_path", "extra"},
                        )
                        if (
                            reference.get("trajectory_id") is None
                            and reference.get("trajectory_path") is None
                        ):
                            fail(field, "subagent references require a document ID or path")
                        for key in ("trajectory_id", "session_id", "trajectory_path"):
                            if reference.get(key) is not None:
                                label(reference[key], field + ".subagent_trajectory_ref." + key)
                        validate_extra(
                            reference.get("extra"), field + ".subagent_trajectory_ref.extra"
                        )
    children = payload.get("subagent_trajectories") or []
    if payload.get("subagent_trajectories") is not None:
        sequence(payload["subagent_trajectories"], "subagent_trajectories")
    ids = set()
    for child in children:
        if not isinstance(child, dict) or child.get("trajectory_id") is None:
            fail("subagent_trajectories", "embedded children require document IDs")
        identity = label(child["trajectory_id"], "subagent_trajectories.trajectory_id")
        if identity in ids:
            fail("subagent_trajectories", "duplicate embedded document identity")
        ids.add(identity)
    return payload
