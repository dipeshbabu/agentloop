from __future__ import annotations

from typing import Any

from agentloop.events import AgentEvent, new_event_id, utc_now_iso
from agentloop.tracer import AgentTrace


def _get(data: Any, key: str, default: Any = None) -> Any:
    if isinstance(data, dict):
        return data.get(key, default)
    return getattr(data, key, default)


def trace_from_vercel_ai_events(
    events: list[dict[str, Any]],
    *,
    name: str = "vercel_ai_run",
    run_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AgentTrace:
    """Convert Vercel AI SDK-style telemetry events into an AgentLoop trace.

    The adapter intentionally accepts plain dictionaries so JS/TS apps can POST or
    export telemetry without importing Python code. Expected fields are flexible:
    `type`/`event_type`, `name`, `duration_ms`, `model`, `input_tokens`,
    `output_tokens`, `status`, and `metadata`.
    """

    trace = AgentTrace(
        name=name, run_id=run_id, metadata={"integration": "vercel_ai", **(metadata or {})}
    )
    for item in events:
        event_type = str(_get(item, "event_type", _get(item, "type", "model_call")))
        if event_type in {"text", "generate", "stream", "model"}:
            event_type = "model_call"
        elif event_type in {"tool", "tool-call", "step"}:
            event_type = "tool_call"

        trace.add_event(
            AgentEvent(
                event_id=str(_get(item, "event_id", new_event_id())),
                run_id=trace.run_id,
                event_type=event_type,
                name=str(_get(item, "name", event_type)),
                started_at=str(_get(item, "started_at", utc_now_iso())),
                ended_at=str(_get(item, "ended_at", utc_now_iso())),
                duration_ms=float(_get(item, "duration_ms", 0.0) or 0.0),
                parent_id=_get(item, "parent_id"),
                model=_get(item, "model"),
                input_tokens=int(_get(item, "input_tokens", _get(item, "prompt_tokens", 0)) or 0),
                output_tokens=int(
                    _get(item, "output_tokens", _get(item, "completion_tokens", 0)) or 0
                ),
                input_text=_get(item, "input_text"),
                output_text=_get(item, "output_text"),
                status=str(_get(item, "status", "ok")),
                error=_get(item, "error"),
                metadata={"integration": "vercel_ai", **(_get(item, "metadata", {}) or {})},
            )
        )
    return trace


TYPESCRIPT_SNIPPET = r"""
// Minimal Vercel AI SDK telemetry bridge for AgentLoop.
// Push these events to your backend, then convert them with
// agentloop.integrations.vercel_ai.trace_from_vercel_ai_events(...).

const agentloopEvents: any[] = [];

export function recordAgentLoopEvent(event: any) {
  agentloopEvents.push({
    ...event,
    started_at: event.started_at ?? new Date().toISOString(),
    ended_at: event.ended_at ?? new Date().toISOString(),
  });
}

export function getAgentLoopEvents() {
  return agentloopEvents;
}
"""
