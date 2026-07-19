from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentloop.integrations.openai_agents import AgentLoopTracingProcessor


def _trace(trace_id: str, name: str = "flow") -> dict[str, Any]:
    return {"trace_id": trace_id, "name": name}


def _span(trace_id: str | None, span_id: str, name: str = "tool") -> dict[str, Any]:
    span: dict[str, Any] = {
        "span_id": span_id,
        "span_data": {"type": "function", "name": name},
        "started_at": "2026-01-01T00:00:00+00:00",
        "ended_at": "2026-01-01T00:00:01+00:00",
    }
    if trace_id is not None:
        span["trace_id"] = trace_id
    return span


class _SdkSpanError:
    def __init__(self, message: str, data: dict[str, Any] | None = None):
        self.message = message
        self.data = data


class _SdkGenerationData:
    type = "generation"
    name = "response"
    model = "gpt-test"
    usage = {"input_tokens": 3, "output_tokens": 5}


class _SdkSpan:
    def __init__(self, trace_id: str, span_id: str, error: _SdkSpanError | None):
        self.trace_id = trace_id
        self.span_id = span_id
        self.span_data = _SdkGenerationData()
        self.error = error
        self.started_at = "2026-01-01T00:00:00+00:00"
        self.ended_at = "2026-01-01T00:00:01+00:00"
        self.parent_id = None


def _export_event(span: Any):
    proc = AgentLoopTracingProcessor()
    trace = _trace("t1")
    proc.on_trace_start(trace)
    proc.on_span_end(span)
    proc.on_trace_end(trace)
    return proc.exported_traces[0], proc.exported_traces[0].events[0]


def test_function_span_error_is_propagated_with_structured_data() -> None:
    span = _span("t1", "failed-tool", name="failing-tool")
    span["error"] = {
        "message": "tool exploded",
        "data": {"type": "RuntimeError", "retryable": False},
    }

    trace, event = _export_event(span)

    assert event.event_type == "tool_call"
    assert event.status == "error"
    assert event.error == "tool exploded"
    assert json.loads(event.metadata["openai_agents.error.data"]) == {
        "retryable": False,
        "type": "RuntimeError",
    }
    json.dumps(trace.to_dict(), allow_nan=False)


def test_sdk_shaped_generation_span_error_is_propagated() -> None:
    span = _SdkSpan(
        "t1",
        "failed-generation",
        _SdkSpanError("model request failed", {"type": "ModelError"}),
    )

    _, event = _export_event(span)

    assert event.event_type == "model_call"
    assert event.model == "gpt-test"
    assert event.status == "error"
    assert event.error == "model request failed"
    assert event.input_tokens == 3
    assert event.output_tokens == 5


def test_span_without_error_remains_ok() -> None:
    _, event = _export_event(_SdkSpan("t1", "generation", None))

    assert event.status == "ok"
    assert event.error is None


def test_nested_error_span_preserves_parent_relationship() -> None:
    proc = AgentLoopTracingProcessor()
    trace = _trace("t1")
    parent = _span("t1", "parent", name="parent-tool")
    child = _span("t1", "child", name="child-tool")
    child["parent_id"] = "parent"
    child["error"] = {"message": "child failed", "data": None}

    proc.on_trace_start(trace)
    proc.on_span_end(parent)
    proc.on_span_end(child)
    proc.on_trace_end(trace)

    events = {event.name: event for event in proc.exported_traces[0].events}
    assert events["child-tool"].parent_id == events["parent-tool"].event_id
    assert events["child-tool"].status == "error"
    assert events["child-tool"].error == "child failed"


def test_empty_error_message_uses_structured_type() -> None:
    span = _span("t1", "failed-tool")
    span["error"] = {"message": "  ", "data": {"type": "RateLimitError"}}

    _, event = _export_event(span)

    assert event.status == "error"
    assert event.error == "OpenAI Agents span failed (RateLimitError)"


def test_error_data_is_normalized_before_json_export(tmp_path: Path) -> None:
    recursive_data: dict[str, Any] = {"exception": RuntimeError("secret details")}
    recursive_data["self"] = recursive_data
    span = _span("t1", "failed-tool")
    span["error"] = {"message": "tool failed", "data": recursive_data}
    proc = AgentLoopTracingProcessor(out_dir=tmp_path)
    trace = _trace("t1")

    proc.on_trace_start(trace)
    proc.on_span_end(span)
    proc.on_trace_end(trace)

    exported = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    metadata = exported["events"][0]["metadata"]
    assert json.loads(metadata["openai_agents.error.data"]) == {
        "exception": "<RuntimeError>",
        "self": "<recursive dict>",
    }


def test_interleaved_traces_export_only_their_own_spans() -> None:
    proc = AgentLoopTracingProcessor()
    proc.on_trace_start(_trace("t1"))
    proc.on_trace_start(_trace("t2"))
    proc.on_span_end(_span("t1", "a"))
    proc.on_span_end(_span("t2", "b"))
    proc.on_trace_end(_trace("t1"))
    proc.on_trace_end(_trace("t2"))

    assert len(proc.exported_traces) == 2
    # Each export carries exactly the one span that belongs to it.
    assert [len(t.events) for t in proc.exported_traces] == [1, 1]


def test_completed_trace_state_is_released() -> None:
    proc = AgentLoopTracingProcessor()
    proc.on_trace_start(_trace("t1"))
    proc.on_span_end(_span("t1", "a"))
    proc.on_trace_end(_trace("t1"))

    assert proc._spans_by_trace == {}
    assert proc._traces == {}
    assert proc._active_trace_ids == []


def test_unassociated_span_attaches_to_single_open_trace() -> None:
    proc = AgentLoopTracingProcessor()
    proc.on_trace_start(_trace("t1"))
    # Span with no trace id arrives while only t1 is open.
    proc.on_span_end(_span(None, "orphan"))
    proc.on_trace_start(_trace("t2"))
    proc.on_trace_end(_trace("t2"))
    proc.on_trace_end(_trace("t1"))

    by_name = {t.events[0].name if t.events else None: len(t.events) for t in proc.exported_traces}
    # t2 (ended first) has no events; t1 owns the single orphan span. Never both.
    assert [len(t.events) for t in proc.exported_traces] == [0, 1]
    assert by_name.get("tool") == 1


def test_unassociated_span_without_open_trace_is_dropped() -> None:
    proc = AgentLoopTracingProcessor()
    proc.on_span_end(_span(None, "orphan"))  # nothing open to attribute it to
    proc.on_trace_start(_trace("t2"))
    proc.on_trace_end(_trace("t2"))

    assert [len(t.events) for t in proc.exported_traces] == [0]


def test_duplicate_trace_end_is_noop() -> None:
    proc = AgentLoopTracingProcessor()
    proc.on_trace_start(_trace("t1"))
    proc.on_span_end(_span("t1", "a"))
    proc.on_trace_end(_trace("t1"))
    proc.on_trace_end(_trace("t1"))  # duplicate

    assert len(proc.exported_traces) == 1


def test_trace_end_without_start_uses_span_trace_id() -> None:
    proc = AgentLoopTracingProcessor()
    proc.on_span_end(_span("t9", "a"))  # no on_trace_start
    proc.on_trace_end(_trace("t9"))

    assert len(proc.exported_traces) == 1
    assert len(proc.exported_traces[0].events) == 1


def test_shutdown_releases_state_and_force_flush_is_safe() -> None:
    proc = AgentLoopTracingProcessor()
    proc.on_trace_start(_trace("t1"))
    proc.on_span_end(_span("t1", "a"))
    proc.shutdown()

    assert proc._traces == {}
    assert proc._spans_by_trace == {}
    assert proc._active_trace_ids == []
    proc.force_flush()  # no error, nothing buffered


def test_completed_traces_do_not_accumulate_state() -> None:
    proc = AgentLoopTracingProcessor(retain_exported=False)
    for index in range(50):
        trace_id = f"t{index}"
        proc.on_trace_start(_trace(trace_id))
        proc.on_span_end(_span(trace_id, "a"))
        proc.on_trace_end(_trace(trace_id))

    # No completed-trace state is retained; retain_exported=False keeps the list empty.
    assert proc._spans_by_trace == {}
    assert proc._traces == {}
    assert proc._active_trace_ids == []
    assert proc.exported_traces == []


def test_out_dir_writes_one_file_per_trace(tmp_path: Path) -> None:
    proc = AgentLoopTracingProcessor(out_dir=tmp_path)
    proc.on_trace_start(_trace("t1"))
    proc.on_span_end(_span("t1", "a"))
    proc.on_trace_end(_trace("t1"))

    written = list(tmp_path.glob("*.json"))
    assert len(written) == 1


def test_export_failure_still_releases_trace_state(tmp_path: Path) -> None:
    # out_dir points at an existing file, so mkdir() raises during on_trace_end.
    out_file = tmp_path / "not-a-dir"
    out_file.write_text("blocker", encoding="utf-8")
    proc = AgentLoopTracingProcessor(out_dir=out_file)
    proc.on_trace_start(_trace("t1"))
    span = _span("t1", "a")
    span["error"] = {"message": "tool failed", "data": {"type": "RuntimeError"}}
    proc.on_span_end(span)

    with pytest.raises(OSError):
        proc.on_trace_end(_trace("t1"))

    # State must still be released so a failed export cannot leak completed-trace memory.
    assert proc._traces == {}
    assert proc._spans_by_trace == {}
    assert proc._active_trace_ids == []
