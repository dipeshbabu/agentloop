from __future__ import annotations

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
    }
    if trace_id is not None:
        span["trace_id"] = trace_id
    return span


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
    proc.on_span_end(_span("t1", "a"))

    with pytest.raises(OSError):
        proc.on_trace_end(_trace("t1"))

    # State must still be released so a failed export cannot leak completed-trace memory.
    assert proc._traces == {}
    assert proc._spans_by_trace == {}
    assert proc._active_trace_ids == []
