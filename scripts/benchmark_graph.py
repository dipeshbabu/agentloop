"""Measure graph analysis on deterministic synthetic traces, without timing gates.

Run with: uv run --frozen python scripts/benchmark_graph.py --memory
Compare the plan_sha256 values between revisions to check output stability.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import platform
import statistics
import time
import tracemalloc
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agentloop.events import AgentEvent
from agentloop.graph import ExecutionGraph
from agentloop.optimizer import build_optimization_plan
from agentloop.tracer import AgentTrace

_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
_SHAPES = ("serial", "parallel", "fanout", "nested")


def _timestamp(offset_ms: int) -> str:
    return (_BASE + timedelta(milliseconds=offset_ms)).isoformat()


def synthetic_trace(shape: str, size: int) -> AgentTrace:
    end_ms = size if shape == "serial" else 1
    trace = AgentTrace(
        f"graph-{shape}",
        run_id=f"benchmark-{shape}-{size}",
        started_at=_timestamp(0),
        ended_at=_timestamp(end_ms),
        elapsed_ms=end_ms,
    )
    for index in range(size):
        parent_id = None
        if index and shape == "fanout":
            parent_id = "node-00000000"
        elif index and shape == "nested":
            parent_id = f"node-{index - 1:08d}"
        start_ms = index if shape == "serial" else 0
        trace.add_event(
            AgentEvent(
                event_id=f"node-{index:08d}",
                run_id=trace.run_id,
                event_type="tool_call",
                name=f"step-{index}",
                started_at=_timestamp(start_ms),
                ended_at=_timestamp(start_ms + 1),
                duration_ms=1,
                parent_id=parent_id,
            )
        )
    return trace


def measure(operation: Callable[[], Any], repeats: int, memory: bool) -> dict[str, float]:
    operation()  # Warm imports and code paths outside the measured samples.
    samples = []
    for _ in range(repeats):
        gc.collect()
        started = time.perf_counter()
        operation()
        samples.append((time.perf_counter() - started) * 1000)
    result = {"median_ms": round(statistics.median(samples), 3)}
    if memory:
        gc.collect()
        tracemalloc.start()
        try:
            operation()
            result["peak_mib"] = round(tracemalloc.get_traced_memory()[1] / (1024 * 1024), 3)
        finally:
            tracemalloc.stop()
    return result


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=_positive_int, nargs="+", default=[1000, 3000])
    parser.add_argument("--shapes", choices=_SHAPES, nargs="+", default=list(_SHAPES))
    parser.add_argument("--repeats", type=_positive_int, default=3)
    parser.add_argument("--memory", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    cases = []
    for size in args.sizes:
        for shape in args.shapes:
            trace = synthetic_trace(shape, size)
            graph = ExecutionGraph.from_trace(trace)
            case = {
                "shape": shape,
                "nodes": size,
                "from_trace": measure(
                    lambda: ExecutionGraph.from_trace(trace), args.repeats, args.memory
                ),
                "critical_path": measure(graph.critical_path, args.repeats, args.memory),
                "optimization_plan": measure(
                    lambda: build_optimization_plan(trace), args.repeats, args.memory
                ),
                "plan_sha256": hashlib.sha256(
                    json.dumps(build_optimization_plan(trace), sort_keys=True).encode()
                ).hexdigest(),
            }
            cases.append(case)
            print(json.dumps(case), flush=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                {
                    "python": platform.python_version(),
                    "platform": platform.platform(),
                    "repeats": args.repeats,
                    "cases": cases,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
