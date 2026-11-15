from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from agentloop.events import AgentEvent
from agentloop.findings import build_diagnosis
from agentloop.tracer import AgentTrace
from examples.codex_exec_runner import convert, task_prompt

# Shape recorded from `codex exec --json` (Codex CLI 0.167.1), shortened.
STREAM = [
    (0.0, {"type": "thread.started", "thread_id": "thread"}),
    (0.5, {"type": "turn.started"}),
    (
        2.0,
        {
            "type": "item.started",
            "item": {"id": "item_1", "type": "command_execution", "command": "cat pyproject.toml"},
        },
    ),
    (
        2.4,
        {
            "type": "item.completed",
            "item": {
                "id": "item_1",
                "type": "command_execution",
                "command": "cat pyproject.toml",
                "aggregated_output": 'requires-python = ">=3.11"',
                "exit_code": 0,
                "status": "completed",
            },
        },
    ),
    (
        3.0,
        {
            "type": "item.completed",
            "item": {"id": "item_2", "type": "agent_message", "text": ">=3.11"},
        },
    ),
    (
        3.1,
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 40000,
                "cached_input_tokens": 32000,
                "output_tokens": 60,
                "reasoning_output_tokens": 20,
            },
        },
    ),
]


def lines(stream):
    return [(moment, json.dumps(event) + "\n") for moment, event in stream]


def run_convert(stream):
    return convert(
        lines(stream),
        run_id="run_test",
        root_id="evt_root",
        origin=datetime(2026, 11, 14, tzinfo=timezone.utc),
        clock=0.0,
        model="test-model",
    )


def test_stream_becomes_tool_and_model_spans_with_turn_usage():
    events, output, usage_seen, failure = run_convert(STREAM)
    assert output == ">=3.11"
    assert usage_seen is True
    assert failure is None
    assert [(event.event_type, event.name) for event in events] == [
        ("model_call", "codex_tool_decision"),
        ("tool_call", "shell"),
        ("model_call", "codex_agent_message"),
    ]
    decision, tool, answer = events
    assert decision.duration_ms == pytest.approx(1500)
    assert tool.duration_ms == pytest.approx(400)
    assert tool.input_text == "cat pyproject.toml"
    assert answer.duration_ms == pytest.approx(600)
    assert decision.input_tokens == 0 and decision.token_provenance == "unavailable"
    assert (answer.input_tokens, answer.output_tokens, answer.token_provenance) == (
        40000,
        60,
        "provider",
    )
    assert answer.metadata["usage_scope"] == "turn_total"
    assert answer.metadata["cached_input_tokens"] == 32000


def test_failed_commands_and_turns_are_retained_as_errors():
    stream = [
        *STREAM[:3],
        (
            2.4,
            {
                "type": "item.completed",
                "item": {"id": "item_1", "type": "command_execution", "exit_code": 2},
            },
        ),
        (3.0, {"type": "turn.failed", "error": {"message": "usage limit reached"}}),
    ]
    events, output, usage_seen, failure = run_convert(stream)
    tool = next(event for event in events if event.event_type == "tool_call")
    assert tool.status == "error" and tool.error == "exit code 2"
    assert output is None
    assert usage_seen is False
    assert failure == "usage limit reached"


def test_usage_without_model_items_gets_its_own_model_span():
    events, _, usage_seen, _ = run_convert([STREAM[1], STREAM[-1]])
    assert usage_seen is True
    assert [(event.name, event.input_tokens) for event in events] == [("codex_turn", 40000)]


def test_converted_events_form_an_analyzable_trace():
    events, output, _, _ = run_convert(STREAM)
    trace = AgentTrace("codex test", metadata={"output": output})
    trace.add_event(
        AgentEvent(
            event_id="evt_root",
            run_id=trace.run_id,
            event_type="agent_step",
            name="codex_exec",
            started_at=events[0].started_at,
            ended_at=events[-1].ended_at,
            duration_ms=3100,
            metadata={"operation_kind": "agent"},
        )
    )
    for event in events:
        event.run_id = trace.run_id
        trace.add_event(event)
    trace.finish()
    restored = AgentTrace.from_dict(json.loads(json.dumps(trace.to_dict())))
    assert build_diagnosis(restored)["run_id"] == trace.run_id


@pytest.mark.parametrize(
    ("value", "instructions", "expected"),
    [
        ("plain", "", "plain"),
        ({"prompt": "object"}, "", "object"),
        ({"question": "legacy"}, "Be brief.", "Be brief.\n\nlegacy"),
    ],
)
def test_task_prompt_accepts_strings_and_prompt_objects(value, instructions, expected):
    assert task_prompt(value, instructions) == expected


def test_task_prompt_rejects_missing_prompts():
    with pytest.raises(ValueError, match="prompt"):
        task_prompt({"other": 1}, "")
