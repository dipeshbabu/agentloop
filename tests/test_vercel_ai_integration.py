from __future__ import annotations

from agentloop.integrations.vercel_ai import trace_from_vercel_ai_events


def test_trace_from_vercel_ai_events_converts_model_and_tool_events() -> None:
    trace = trace_from_vercel_ai_events(
        [
            {
                "type": "generate",
                "name": "generateText",
                "duration_ms": 120,
                "model": "gpt-test",
                "prompt_tokens": 11,
                "completion_tokens": 6,
            },
            {"type": "tool-call", "name": "search", "duration_ms": 80, "metadata": {"tool": "web"}},
        ],
        name="vercel_test",
    )
    assert trace.name == "vercel_test"
    assert len(trace.events) == 2
    assert trace.events[0].event_type == "model_call"
    assert trace.events[0].input_tokens == 11
    assert trace.events[0].output_tokens == 6
    assert trace.events[1].event_type == "tool_call"
    assert trace.events[1].metadata["tool"] == "web"
