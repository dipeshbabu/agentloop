from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from agentloop.events import AgentEvent
from agentloop.findings import build_diagnosis
from agentloop.runners.claude_code import convert, task_prompt
from agentloop.tracer import AgentTrace

USAGE = {
    "input_tokens": 12,
    "cache_creation_input_tokens": 3000,
    "cache_read_input_tokens": 15000,
    "output_tokens": 80,
}


def assistant(message_id, *content, usage=USAGE):
    return {
        "type": "assistant",
        "message": {
            "id": message_id,
            "model": "claude-test",
            "content": list(content),
            "usage": usage,
        },
    }


def tool_use(tool_id, name, value):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": value}


def tool_result(tool_id, text, is_error=False):
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": text,
                    "is_error": is_error,
                }
            ],
        },
    }


INIT = {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-test"}
RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": ">=3.11",
    "num_turns": 2,
    "duration_ms": 3000,
    "duration_api_ms": 2500,
    "total_cost_usd": 0.0123,
}

# Shape of `claude -p --output-format stream-json --verbose`, shortened. Claude
# Code may emit one assistant event per content block of the same response.
STREAM = [
    (0.5, INIT),
    (1.5, assistant("msg_1", {"type": "text", "text": "Checking."})),
    (2.0, assistant("msg_1", tool_use("toolu_1", "Read", {"file_path": "pyproject.toml"}))),
    (2.3, tool_result("toolu_1", 'requires-python = ">=3.11"')),
    (3.0, assistant("msg_2", {"type": "text", "text": ">=3.11"})),
    (3.1, RESULT),
]


def run_convert(stream):
    return convert(
        [(moment, json.dumps(event) + "\n") for moment, event in stream],
        run_id="run_test",
        root_id="evt_root",
        origin=datetime(2026, 11, 19, tzinfo=timezone.utc),
        clock=0.0,
        model=None,
    )


def test_each_response_is_one_model_span_and_each_tool_use_a_tool_span():
    events, output, usage_seen, failure, details = run_convert(STREAM)
    assert (output, usage_seen, failure) == (">=3.11", True, None)
    assert [(event.event_type, event.name) for event in events] == [
        ("model_call", "claude_response"),
        ("tool_call", "Read"),
        ("model_call", "claude_response"),
    ]
    decision, tool, answer = events
    assert decision.duration_ms == pytest.approx(1500)
    assert decision.metadata["content_types"] == ["text", "tool_use"]
    assert tool.duration_ms == pytest.approx(300)
    assert json.loads(tool.input_text) == {"file_path": "pyproject.toml"}
    assert tool.output_text == 'requires-python = ">=3.11"'
    assert answer.duration_ms == pytest.approx(700)
    assert answer.output_text == ">=3.11"
    assert details["claude_total_cost_usd"] == 0.0123
    assert details["claude_num_turns"] == 2
    assert details["claude_session_id"] == "s1"


def test_usage_is_set_once_per_response_and_includes_cache_tokens():
    events, *_ = run_convert(STREAM)
    decision = events[0]
    assert decision.input_tokens == 12 + 3000 + 15000
    assert decision.output_tokens == 80
    assert decision.token_provenance == "provider"
    assert decision.model == "claude-test"
    assert decision.metadata["usage_scope"] == "request"
    assert decision.metadata["cached_input_tokens"] == 15000
    assert decision.metadata["cache_creation_input_tokens"] == 3000
    assert "start" not in decision.metadata


def test_parallel_tool_uses_share_one_model_span():
    stream = [
        STREAM[0],
        (
            1.0,
            assistant(
                "msg_1",
                tool_use("a", "Grep", {"pattern": "x"}),
                tool_use("b", "Glob", {"pattern": "*.py"}),
            ),
        ),
        (1.4, tool_result("a", "hit")),
        (1.6, tool_result("b", [{"type": "text", "text": "a.py"}])),
        (2.6, assistant("msg_2", {"type": "text", "text": "done"})),
    ]
    events, *_ = run_convert(stream)
    assert [event.event_type for event in events] == [
        "model_call",
        "tool_call",
        "tool_call",
        "model_call",
    ]
    assert events[2].output_text == "a.py"
    assert events[3].duration_ms == pytest.approx(1000)


def test_tool_errors_and_failed_results_are_retained():
    stream = [
        STREAM[0],
        (1.0, assistant("msg_1", tool_use("t", "Bash", {"command": "rm -rf x"}))),
        (1.1, tool_result("t", "Permission to use Bash has been denied.", is_error=True)),
        (
            2.0,
            {
                "type": "result",
                "subtype": "error_max_turns",
                "is_error": True,
                "num_turns": 1,
            },
        ),
    ]
    events, output, _, failure, _ = run_convert(stream)
    tool = next(event for event in events if event.event_type == "tool_call")
    assert tool.status == "error" and "denied" in tool.error
    assert output is None
    assert failure == "error_max_turns"


def test_converted_events_form_an_analyzable_trace():
    events, output, *_ = run_convert(STREAM)
    trace = AgentTrace("claude test", metadata={"output": output})
    trace.add_event(
        AgentEvent(
            event_id="evt_root",
            run_id=trace.run_id,
            event_type="agent_step",
            name="claude_code",
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


FAKE_CLAUDE = """import json, os, sys
prompt = sys.stdin.read()
record = {"argv": sys.argv[1:], "cwd": os.getcwd(), "prompt": prompt}
with open(os.environ["FAKE_CLAUDE_RECORD"], "w", encoding="utf-8") as handle:
    json.dump(record, handle)
for event in EVENTS:
    print(json.dumps(event), flush=True)
"""


def test_runner_drives_the_cli_and_writes_a_successful_trace(tmp_path):
    script = tmp_path / "fake_claude.py"
    events = [event for _, event in STREAM]
    script.write_text(f"EVENTS = {events!r}\n" + FAKE_CLAUDE, encoding="utf-8")
    if os.name == "nt":
        executable = tmp_path / "fake-claude.cmd"
        executable.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        executable = tmp_path / "fake-claude"
        executable.write_text(
            f"#!{sys.executable}\n" + script.read_text(encoding="utf-8"), encoding="utf-8"
        )
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    record = tmp_path / "record.json"
    trace_path = tmp_path / "trace.json"
    request = {
        "task_id": "python",
        "repetition": 0,
        "input": {"prompt": "Which Python?"},
        "trace_path": str(trace_path),
    }
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "agentloop.runners.claude_code",
            "--claude",
            str(executable),
            "--cd",
            str(workdir),
            "--model",
            "claude-test",
            "--condition",
            "baseline",
            "--",
            "--max-turns",
            "4",
        ],
        input=json.dumps(request) + "\n",
        capture_output=True,
        text=True,
        env={**os.environ, "FAKE_CLAUDE_RECORD": str(record)},
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    seen = json.loads(record.read_text(encoding="utf-8"))
    assert seen["argv"] == [
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--allowedTools",
        "Read,Grep,Glob",
        "--model",
        "claude-test",
        "--max-turns",
        "4",
    ]
    assert os.path.samefile(seen["cwd"], workdir)
    assert seen["prompt"] == "Which Python?"
    trace = AgentTrace.from_dict(json.loads(trace_path.read_text(encoding="utf-8")))
    assert trace.metadata["output"] == ">=3.11"
    assert trace.metadata["success"] is True
    assert trace.metadata["integration"] == "claude_code"
    kinds = [event.event_type for event in trace.events]
    assert kinds.count("model_call") == 2 and kinds.count("tool_call") == 1


@pytest.mark.parametrize(
    ("module", "example", "option"),
    [
        ("agentloop.runners.codex", "examples/codex_exec_runner.py", "--codex"),
        ("agentloop.runners.claude_code", "examples/claude_code_runner.py", "--claude"),
    ],
)
def test_packaged_runners_and_example_scripts_share_one_entry_point(module, example, option):
    for command in ([sys.executable, "-m", module], [sys.executable, example]):
        completed = subprocess.run(  # noqa: S603
            [*command, "--help"], capture_output=True, text=True, timeout=60, check=False
        )
        assert completed.returncode == 0, completed.stderr
        assert option in completed.stdout
        assert f"python -m {module}" in completed.stdout
