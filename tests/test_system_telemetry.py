"""Optional process, subprocess and inference telemetry (issue #405)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from agentloop import system_telemetry
from agentloop.system_telemetry import (
    INFERENCE_KEY,
    SYSTEM_KEY,
    StreamTimer,
    cached_input_tokens,
    resource_span,
    run_subprocess,
    sample_process,
)
from agentloop.tracer import trace_agent, trace_tool_call


def test_default_spans_are_unchanged():
    with trace_agent("plain") as trace, trace_tool_call("work"):
        pass
    assert SYSTEM_KEY not in trace.events[0].metadata


def test_sample_reports_values_or_reasons():
    sample = sample_process()
    assert sample["cpu_user_s"] >= 0 and sample["monotonic_ns"] > 0
    for key in ("rss_bytes", "io", "children_cpu_s"):
        if sample[key] is None:
            assert sample["unavailable"], key
    if os.name == "nt":
        assert sample["unavailable"]["children_cpu"] == "platform"


def test_resource_span_records_cpu_and_memory():
    with trace_agent("work") as trace, resource_span("crunch", metadata={"stage": "x"}):
        deadline = time.process_time() + 0.05
        while time.process_time() < deadline:
            sum(range(1000))
    event = trace.events[0]
    system = event.metadata[SYSTEM_KEY]
    assert event.metadata["stage"] == "x"
    assert system["sampled"] is True and system["clock"] == "monotonic"
    assert system["process_cpu_ms"] >= 30
    assert system["wall_ms"] >= 30
    assert system["pid"] == os.getpid()


def test_resource_span_records_failures_too():
    with trace_agent("work") as trace:
        with pytest.raises(ValueError), resource_span("fails"):
            raise ValueError("boom")
    event = trace.events[0]
    assert event.status == "error" and event.metadata[SYSTEM_KEY]["sampled"] is True


def test_sampling_is_deterministic_per_name():
    with trace_agent("sampled") as trace:
        for _ in range(6):
            with resource_span("tick", sample_every=3):
                pass
    flags = [e.metadata[SYSTEM_KEY]["sampled"] for e in trace.events]
    assert flags == [True, False, False, True, False, False]
    with pytest.raises(ValueError), trace_agent("bad"), resource_span("x", sample_every=0):
        pass


def test_missing_permissions_are_reported(monkeypatch):
    if not sys.platform.startswith("linux"):
        pytest.skip("/proc is Linux-only")

    def denied():
        return None, "permission_denied"

    monkeypatch.setattr(system_telemetry, "_linux_io", denied)
    assert sample_process()["unavailable"]["io"] == "permission_denied"


def test_windows_memory_failure_is_reported(monkeypatch):
    monkeypatch.setattr(system_telemetry.os, "name", "nt")
    monkeypatch.setattr(system_telemetry, "_windows_memory", lambda: (None, None, "api_failed"))
    sample = sample_process()
    assert sample["rss_bytes"] is None
    assert sample["unavailable"]["memory"] == "api_failed"


def test_subprocess_lifecycle_without_arguments():
    secret = "--token=sk-very-secret"
    with trace_agent("sub") as trace:
        completed = run_subprocess([sys.executable, "-c", "import sys; sys.exit(3)", secret])
    assert completed.returncode == 3
    event = trace.events[0]
    lifecycle = event.metadata[SYSTEM_KEY]["subprocess"]
    assert lifecycle["exit_code"] == 3 and lifecycle["timed_out"] is False
    assert lifecycle["argument_count"] == 3
    assert secret not in repr(event.metadata) and secret not in event.name
    if os.name == "nt":
        assert lifecycle["children_cpu_ms"] is None
        assert lifecycle["unavailable"] == {"children_cpu": "platform"}


def test_subprocess_timeout_and_start_failure_are_recorded():
    with trace_agent("sub") as trace:
        with pytest.raises(subprocess.TimeoutExpired):
            run_subprocess([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)
        with pytest.raises(OSError):
            run_subprocess(["definitely-not-a-real-command-agentloop"])
    timed_out, missing = (e.metadata[SYSTEM_KEY]["subprocess"] for e in trace.events)
    assert timed_out["timed_out"] is True and timed_out["exit_code"] is None
    assert "start_error" in missing
    with pytest.raises(ValueError):
        run_subprocess("python -c 1")


def test_stream_timer_measures_first_token():
    timer = StreamTimer()
    time.sleep(0.02)
    timer.token()
    timer.token()
    timer.finish()
    value = timer.metadata()[INFERENCE_KEY]
    assert value["time_to_first_token_ms"] >= 15
    assert value["chunks"] == 2 and value["stream_complete"] is True
    assert value["total_ms"] >= value["time_to_first_token_ms"]


def test_incomplete_stream_reports_missing_values():
    timer = StreamTimer()
    value = timer.metadata()[INFERENCE_KEY]
    assert value["time_to_first_token_ms"] is None
    assert value["unavailable"] == {"time_to_first_token": "no_tokens"}
    assert value["stream_complete"] is False and "total_ms" not in value


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        ({"prompt_tokens_details": {"cached_tokens": 512}}, 512),
        (SimpleNamespace(prompt_tokens_details=SimpleNamespace(cached_tokens=0)), 0),
        ({"input_tokens_details": {"cached_tokens": 64}}, 64),
        ({"cache_read_input_tokens": 1024}, 1024),
        ({"prompt_tokens": 10}, None),
        ({"prompt_tokens_details": {"cached_tokens": -1}}, None),
        ({"prompt_tokens_details": {"cached_tokens": True}}, None),
        (None, None),
    ],
)
def test_cached_tokens_are_read_only_when_reported(usage, expected):
    assert cached_input_tokens(usage) == expected


@pytest.mark.parametrize("work_ms", [0, 20])
def test_collector_overhead_is_small(work_ms):
    """Short and long tasks: sampling costs well under a millisecond per span."""

    def task():
        if work_ms:
            time.sleep(work_ms / 1000)

    def run(wrapper, count=50):
        start = time.perf_counter()
        with trace_agent("overhead"):
            for _ in range(count):
                with wrapper("t"):
                    task()
        return (time.perf_counter() - start) / count

    plain = run(trace_tool_call)
    measured = run(resource_span)
    assert measured - plain < 0.005  # 5 ms ceiling; typical cost is tens of microseconds
