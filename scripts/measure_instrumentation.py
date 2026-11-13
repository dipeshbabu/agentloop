"""Counterbalanced offline recording controls with separate export/analysis timings."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import time
from pathlib import Path

import agentloop
from agentloop.entrypoint import _analysis_payload
from agentloop.improvement import write_once
from agentloop.studies import summarize_values
from agentloop.tracer import AgentTrace, bind_trace_context, trace_tool_call
from agentloop.version import __version__


def workload(size, traced):
    trace = (
        AgentTrace(
            "instrumentation-control",
            metadata={"synthetic": True, "source": "instrumentation-control"},
        )
        if traced
        else None
    )
    total = 0
    if trace is None:
        for index in range(size):
            total += index * index
    else:
        with bind_trace_context(trace):
            for index in range(size):
                with trace_tool_call("square"):
                    total += index * index
        trace.metadata["output"] = total
        trace.finish()
    return total, trace


def timed(operation):
    started = time.perf_counter_ns()
    result = operation()
    return result, (time.perf_counter_ns() - started) / 1_000_000


def run(out, *, source_revision, sizes=(32, 256, 1024), repetitions=6):
    if not sizes or any(type(size) is not int or size < 1 for size in sizes):
        raise ValueError("sizes must contain positive integers")
    if len(set(sizes)) != len(sizes):
        raise ValueError("sizes must be distinct")
    if type(repetitions) is not int or repetitions < 2 or repetitions % 2:
        raise ValueError("use a positive even repetition count of at least two for balanced order")
    if len(source_revision) != 40 or any(
        char not in "0123456789abcdef" for char in source_revision
    ):
        raise ValueError("source_revision must identify the forty-character source commit")
    root = Path(out)
    root.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve()
    package = Path(agentloop.__file__).resolve().parent
    sources = {
        path.relative_to(package).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(package.rglob("*.py"))
    }
    rows = [
        {
            "size": size,
            "repetition": repetition,
            "order": [True, False] if repetition % 2 == 0 else [False, True],
        }
        for size in sizes
        for repetition in range(repetitions)
    ]
    protocol = {
        "schema_version": "1.0",
        "synthetic": True,
        "source_revision": source_revision,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "package_sources": sources,
        "agentloop_version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "sizes": list(sizes),
        "repetitions": repetitions,
        "planned_pairs": rows,
        "cost_usd": None,
        "scope": "Deterministic CPU fixture. Recording includes context/span setup and finalization. Export includes JSON serialization and a buffered local write. Analysis uses the native analysis pipeline. Model, transport and billing evidence are unavailable.",
    }
    write_once(root, "protocol.json", protocol)
    # Warm code paths outside the samples. The protocol remains if warmup fails.
    for size in sizes:
        workload(size, False)
        _, trace = workload(size, True)
        trace.to_dict()
        _analysis_payload(trace)
    attempts = []
    for planned in rows:
        row = {**planned, "status": "running", "qualified": False, "cost_usd": None}
        case = root / f"spans-{row['size']}-repeat-{row['repetition']}"
        case.mkdir()
        try:
            results = {}
            for traced in row["order"]:
                (answer, trace), elapsed = timed(lambda: workload(row["size"], traced))
                results[traced] = (answer, trace)
                row["traced_ms" if traced else "untraced_ms"] = elapsed
            answer, trace = results[True]
            row["expected"] = row["size"] * (row["size"] - 1) * (2 * row["size"] - 1) // 6
            row["output"] = answer
            row["matching_outputs"] = answer == results[False][0]
            _, row["export_ms"] = timed(lambda: trace.export_json(case / "trace.json"))
            loaded = AgentTrace.from_json(case / "trace.json")
            analysis, row["analysis_ms"] = timed(lambda: _analysis_payload(loaded))
            write_once(case, "analysis.json", analysis)
            row["qualified"] = (
                row["matching_outputs"]
                and answer == row["expected"]
                and len(loaded.events) == row["size"]
                and all(event.status == "ok" for event in loaded.events)
                and analysis["report"]["analysis_complete"] is True
            )
            row["difference_ms"] = row["traced_ms"] - row["untraced_ms"]
            row["status"] = "completed"
        except Exception as error:
            row.update(status="failed", error_type=type(error).__name__)
        except BaseException:
            row["status"] = "cancelled"
            raise
        finally:
            write_once(case, "attempt.json", row)
        attempts.append(row)
    summaries = []
    for size in sizes:
        selected = [row for row in attempts if row["size"] == size]
        summaries.append(
            {
                "spans": size,
                "planned_pairs": repetitions,
                "qualified_pairs": sum(row["qualified"] for row in selected),
                **{
                    name: summarize_values(
                        [row.get(name) if row["qualified"] else None for row in selected]
                    )
                    for name in (
                        "traced_ms",
                        "untraced_ms",
                        "difference_ms",
                        "export_ms",
                        "analysis_ms",
                    )
                },
            }
        )
    report = {
        "protocol": protocol,
        "attempts": attempts,
        "summaries": summaries,
        "limits": "Repeated controls on one shared host are descriptive. Application-owned agents, independent task coverage and operating costs remain unmeasured. Raw timings and failed attempts are retained. Export and analysis each have their own measured scope.",
    }
    write_once(root, "summary.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--sizes", type=int, nargs="+", default=[32, 256, 1024])
    parser.add_argument("--repetitions", type=int, default=6)
    args = parser.parse_args()
    report = run(
        args.out,
        source_revision=args.source_revision,
        sizes=args.sizes,
        repetitions=args.repetitions,
    )
    print(json.dumps(report["summaries"], indent=2))
    return 0 if all(row["qualified"] for row in report["attempts"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
