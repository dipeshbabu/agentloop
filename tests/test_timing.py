from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import agentloop.tracer as tracer_module
from agentloop.events import AgentEvent, utc_now_iso
from agentloop.graph import ExecutionGraph
from agentloop.otel import trace_from_otel
from agentloop.tracer import AgentTrace, trace_agent, trace_model_call, trace_tool_call

_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _Clock:
    def __init__(self) -> None:
        self.current_s = 0.0

    def perf_counter(self) -> float:
        return self.current_s

    def utc_now_iso(self) -> str:
        return (_BASE + timedelta(seconds=self.current_s)).isoformat()

    def advance(self, seconds: float) -> None:
        self.current_s += seconds


def _install_clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(tracer_module, "time", SimpleNamespace(perf_counter=clock.perf_counter))
    monkeypatch.setattr(tracer_module, "utc_now_iso", clock.utc_now_iso)
    return clock


def _timestamp(offset_ms: float) -> str:
    return (_BASE + timedelta(milliseconds=offset_ms)).isoformat()


def _event(
    trace: AgentTrace,
    event_id: str,
    started_ms: float,
    ended_ms: float,
    *,
    parent_id: str | None = None,
) -> AgentEvent:
    return AgentEvent(
        event_id=event_id,
        run_id=trace.run_id,
        event_type="tool_call",
        name=event_id,
        started_at=_timestamp(started_ms),
        ended_at=_timestamp(ended_ms),
        duration_ms=ended_ms - started_ms,
        parent_id=parent_id,
    )


def test_native_trace_records_uninstrumented_elapsed_time_and_round_trips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _install_clock(monkeypatch)
    with trace_agent("sleep-only") as trace:
        clock.advance(0.03)

    report = trace.report()
    payload = trace.to_dict()
    restored = AgentTrace.from_dict(payload)

    assert trace.ended_at is not None
    assert trace.elapsed_ms == pytest.approx(30)
    assert report["total_runtime_ms"] == 30
    assert report["cumulative_span_time_ms"] == 0
    assert payload["ended_at"] == trace.ended_at
    assert payload["elapsed_ms"] == trace.elapsed_ms
    assert restored.report()["total_runtime_ms"] == report["total_runtime_ms"]


def test_concurrent_events_do_not_inflate_native_elapsed_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _install_clock(monkeypatch)

    async def worker(name: str, started: asyncio.Event, release: asyncio.Event) -> None:
        with trace_tool_call(name):
            started.set()
            await release.wait()

    async def run_workers() -> None:
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        release = asyncio.Event()
        first = asyncio.create_task(worker("first", first_started, release))
        second = asyncio.create_task(worker("second", second_started, release))
        await first_started.wait()
        await second_started.wait()
        clock.advance(0.1)
        release.set()
        await asyncio.gather(first, second)

    with trace_agent("async-concurrency") as trace:
        asyncio.run(run_workers())

    report = trace.report()
    assert len(trace.events) == 2
    assert all(event.parent_id is None for event in trace.events)
    assert report["total_runtime_ms"] == 100
    assert report["cumulative_span_time_ms"] == 200


def test_native_nested_events_receive_parent_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _install_clock(monkeypatch)
    with trace_agent("nested") as trace:
        with trace_tool_call("outer"):
            clock.advance(0.01)
            with trace_model_call("inner"):
                clock.advance(0.02)
            clock.advance(0.01)

    outer = next(event for event in trace.events if event.name == "outer")
    inner = next(event for event in trace.events if event.name == "inner")
    graph = ExecutionGraph.from_trace(trace)

    assert inner.parent_id == outer.event_id
    assert any(
        edge.source == outer.event_id and edge.target == inner.event_id and edge.kind == "parent"
        for edge in graph.edges
    )
    assert graph.critical_path().duration_ms <= graph.total_runtime_ms() * 1.1


def test_timestamp_envelope_handles_concurrent_nested_and_sequential_events() -> None:
    trace = AgentTrace(
        "timestamped",
        started_at=_timestamp(0),
        ended_at=_timestamp(400),
    )
    trace.add_event(_event(trace, "outer", 0, 300))
    trace.add_event(_event(trace, "child", 50, 150, parent_id="outer"))
    trace.add_event(_event(trace, "next", 300, 400))

    report = trace.report()
    graph = ExecutionGraph.from_trace(trace)

    assert report["total_runtime_ms"] == 400
    assert report["cumulative_span_time_ms"] == 500
    assert graph.total_runtime_ms() == 400
    assert graph.critical_path().duration_ms == pytest.approx(400)
    assert graph.critical_path().node_ids == ["outer", "next"]


def test_legacy_trace_with_inconsistent_timestamps_falls_back_to_span_sum() -> None:
    trace = AgentTrace("legacy")
    now = utc_now_iso()
    for index, duration in enumerate((100.0, 50.0)):
        trace.add_event(
            AgentEvent(
                event_id=f"legacy-{index}",
                run_id=trace.run_id,
                event_type="tool_call",
                name=f"legacy-{index}",
                started_at=now,
                ended_at=now,
                duration_ms=duration,
            )
        )

    report = trace.report()

    assert report["total_runtime_ms"] == 150
    assert report["cumulative_span_time_ms"] == 150


def test_out_of_order_otel_spans_use_timestamp_and_parent_order() -> None:
    spans = [
        {
            "traceId": "1" * 32,
            "spanId": "c" * 16,
            "parentSpanId": "b" * 16,
            "name": "third",
            "startTimeUnixNano": "1200000000",
            "endTimeUnixNano": "1300000000",
        },
        {
            "traceId": "1" * 32,
            "spanId": "a" * 16,
            "name": "first",
            "startTimeUnixNano": "1000000000",
            "endTimeUnixNano": "1100000000",
        },
        {
            "traceId": "1" * 32,
            "spanId": "b" * 16,
            "parentSpanId": "a" * 16,
            "name": "second",
            "startTimeUnixNano": "1100000000",
            "endTimeUnixNano": "1200000000",
        },
    ]

    trace = trace_from_otel(spans)
    graph = ExecutionGraph.from_trace(trace)

    assert trace.report()["total_runtime_ms"] == 300
    assert [node.name for node in graph.nodes] == ["first", "second", "third"]
    assert graph.critical_path().node_ids == [
        "span_" + "a" * 16,
        "span_" + "b" * 16,
        "span_" + "c" * 16,
    ]
    assert graph.critical_path().duration_ms == 300


def test_concurrent_otel_spans_use_temporal_envelope() -> None:
    spans = [
        {
            "traceId": "2" * 32,
            "spanId": span_id * 16,
            "name": name,
            "startTimeUnixNano": "1000000000",
            "endTimeUnixNano": "1100000000",
        }
        for span_id, name in (("a", "first"), ("b", "second"))
    ]

    trace = trace_from_otel(spans)
    graph = ExecutionGraph.from_trace(trace)

    assert trace.report()["total_runtime_ms"] == 100
    assert trace.report()["cumulative_span_time_ms"] == 200
    assert graph.critical_path().duration_ms == 100
