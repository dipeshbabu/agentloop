"""Measure bounded offline import/export/analysis on generated synthetic sources.

Use the same --inputs directory with two Python environments to compare revisions.
Generation, startup and reproducibility rechecks are outside phase measurements.
"""

from __future__ import annotations

import argparse
import gc
import json
import platform
import time
import tracemalloc
from hashlib import sha256
from pathlib import Path

import agentloop
from agentloop.integrations.harbor.trials import HarborOptions, import_harbor
from agentloop.integrations.harbor.verifier import ScoringContract
from agentloop.integrations.omnigent.telemetry import import_omnigent
from agentloop.interoperability.artifacts import native_bytes
from agentloop.interoperability.otlp_jsonl import OtlpOptions, import_otlp
from agentloop.interoperability.validation import ImportLimits
from agentloop.interventions import canonical_json


def _write_owned(path: Path, payload) -> None:
    data = (canonical_json(payload) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != data:
        raise ValueError("owned generated input has conflicting bytes")
    path.write_bytes(data)


def _span(trace_id, span_id, *, parent=None, start=0, end=1, session=None, kind="AGENT"):
    attrs = {"openinference.span.kind": kind}
    if session:
        attrs["session.id"] = session
    if kind == "LLM":
        attrs.update({"gen_ai.usage.input_tokens": 12, "gen_ai.usage.output_tokens": 6})
    value = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": kind.lower(),
        "startTimeUnixNano": str(1767225600000000000 + start * 1_000_000),
        "endTimeUnixNano": str(1767225600000000000 + end * 1_000_000),
        "status": {"code": "STATUS_CODE_OK"},
        "attributes": [
            {
                "key": key,
                "value": {"intValue": str(item)} if type(item) is int else {"stringValue": item},
            }
            for key, item in attrs.items()
        ],
    }
    if parent:
        value["parentSpanId"] = parent
    return value


def _record(spans):
    return {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}


def generate_sources(root: Path, size: int) -> None:
    fixture = Path(__file__).resolve().parents[1] / "tests/fixtures/external/harbor/mixed_job/pass"
    templates = {
        path.relative_to(fixture).as_posix(): json.loads(path.read_text(encoding="utf-8"))
        for path in fixture.rglob("*.json")
    }
    for ordinal in range(size):
        name = f"owned-trial-{ordinal:04d}"
        quality = 0 if ordinal % 7 == 0 else 1
        for reference, original in templates.items():
            if reference == "agent/trajectory.json" and ordinal % 10 == 0:
                continue
            value = json.loads(json.dumps(original))
            if reference == "result.json":
                value.update(
                    id=name, trial_name=name, repetition=ordinal, protocol_id="owned-scalability-v1"
                )
                value["agent_result"] = {
                    "n_input_tokens": 12,
                    "n_output_tokens": 6,
                    "n_cache_tokens": 4,
                    "cost_usd": 0.1,
                }
                value["verifier_result"] = {"rewards": {"correctness": quality}}
                if ordinal % 17 == 0:
                    value["exception_info"] = {
                        "exception_type": "TrialTimeoutError",
                        "exception_message": "owned timeout",
                    }
            if reference == "verifier/reward.json":
                value = {"correctness": quality}
            _write_owned(root / "harbor" / name / reference, value)
    _write_owned(
        root / "harbor/result.json", {"id": f"owned-job-{size}", "n_total_trials": size + 1}
    )
    many_traces, large_trace = [], []
    for ordinal in range(size):
        trace_id = f"{ordinal + 1:032x}"
        many_traces.append(
            _record(
                [
                    _span(trace_id, "1" * 16, end=10, session=f"owned-session-{ordinal}"),
                    _span(trace_id, "2" * 16, parent="1" * 16, start=1, end=9, kind="TOOL"),
                ]
            )
        )
        spans = [
            _span(
                "f" * 32,
                f"{ordinal + 2:016x}",
                parent="1" * 16,
                start=ordinal,
                end=ordinal + 1,
                kind="LLM",
            )
        ]
        if ordinal == 0:
            spans.insert(0, _span("f" * 32, "1" * 16, end=size))
        large_trace.append(_record(spans))
    for filename, records in (("omnigent.jsonl", many_traces), ("large-trace.jsonl", large_trace)):
        data = "".join(canonical_json(record) + "\n" for record in records).encode()
        path = root / filename
        if path.exists() and path.read_bytes() != data:
            raise ValueError("owned generated JSONL has conflicting bytes")
        path.write_bytes(data)


def _measure(action):
    gc.collect()
    tracemalloc.start()
    wall, cpu = time.perf_counter(), time.process_time()
    try:
        result = action()
        measurement = {
            "wall_seconds": time.perf_counter() - wall,
            "cpu_seconds": time.process_time() - cpu,
            "peak_tracemalloc_bytes": tracemalloc.get_traced_memory()[1],
        }
    finally:
        tracemalloc.stop()
    return result, measurement


def _fingerprint(result) -> str:
    receipts = getattr(result, "source_receipts", None)
    traces = result.traces
    if receipts is None:
        receipts = (*result.trajectory_receipts, *result.trial_receipts)
        traces = (*traces, *result.trial_traces)
    value = {
        "inventory": result.inventory(),
        "receipts": [row.to_dict() for row in receipts],
        "traces": [sha256(native_bytes(trace)).hexdigest() for trace in traces],
    }
    return sha256(canonical_json(value).encode()).hexdigest()


def measure_sources(root: Path, out: Path, size: int, revision: str) -> dict:
    limits = ImportLimits(max_trajectories=max(256, size * 2), max_references=max(1024, size * 8))
    loaders = {
        "harbor_job": lambda: import_harbor(
            root / "harbor",
            options=HarborOptions(
                scoring=ScoringContract.all_gte("owned-correctness-v1", {"correctness": 1}),
                synthetic_fixture=True,
            ),
            limits=limits,
        ),
        "omnigent_sessions": lambda: import_omnigent(
            root / "omnigent.jsonl",
            options=OtlpOptions(system="omnigent", synthetic_fixture=True),
            limits=limits,
        ),
        "otlp_large_trace": lambda: import_otlp(
            root / "large-trace.jsonl", options=OtlpOptions(synthetic_fixture=True), limits=limits
        ),
    }
    workloads = {}
    for name, loader in loaders.items():
        result, import_metrics = _measure(loader)
        fingerprint = _fingerprint(result)
        repeated = loader()
        assert _fingerprint(repeated) == fingerprint
        del repeated
        _, export_metrics = _measure(
            lambda captured=result, folder=out / name: captured.write(folder)
        )
        result.write(out / name)
        inventory = result.inventory()
        row = {
            "import": import_metrics,
            "export": export_metrics,
            "repeat_import_sha256": fingerprint,
            "repeat_export": "idempotent",
            "native_traces": len(result.traces),
        }
        if name == "harbor_job":
            assert inventory["trials_discovered"] == size
            assert inventory["planned_unidentified_trials"] == 1
            row["coverage"] = {
                key: inventory[key]
                for key in (
                    "trials_discovered",
                    "trials_planned",
                    "missing_trajectories",
                    "quality_pass",
                    "quality_fail",
                    "quality_indeterminate",
                )
            }
        if name == "omnigent_sessions":
            assert inventory["session_groups"] == size
            row["session_groups"] = inventory["session_groups"]
        if name == "otlp_large_trace":
            assert len(result.traces) == 1 and len(result.traces[0].events) == size + 1
            report, analysis_metrics = _measure(result.traces[0].report)
            row["analysis"] = analysis_metrics
            row["recorded_model_blocks"] = report["reported_model_call_count"]
            assert report["reported_model_call_count"] == size
        workloads[name] = row
        del result
    package = Path(agentloop.__file__).resolve().parent
    measured_modules = [
        "integrations/harbor/trials.py",
        "integrations/omnigent/telemetry.py",
        "interoperability/otlp_jsonl.py",
        "interoperability/coordination.py",
    ]
    return {
        "schema_version": "1.0",
        "synthetic": True,
        "source_revision": revision,
        "size": size,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "module_sha256": {
            name: sha256((package / name).read_bytes()).hexdigest() for name in measured_modules
        },
        "limits": vars(limits),
        "workloads": workloads,
        "measurement_scope": "local parser/export/report overhead; generation, startup, repeated-import fingerprint and repeated-export checks excluded",
        "limitations": [
            "single samples on a shared host; filesystem cache and competing CPU load uncontrolled",
            "tracemalloc measures Python allocation peaks, not whole-process RSS",
            "source runtimes and rewards are owned synthetic data; no provider or task-performance benefit established",
            "large cases explicitly raise bounded trajectory/reference caps; core defaults unchanged",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--out", type=Path, default=Path("runs/interoperability-scalability"))
    parser.add_argument("--sizes", type=int, nargs="+", default=[10, 100, 1000])
    parser.add_argument("--revision", default="working-tree")
    parser.add_argument("--generate-only", action="store_true")
    parser.add_argument("--measure-only", action="store_true")
    args = parser.parse_args()
    if (
        args.generate_only
        and args.measure_only
        or any(not 1 <= size <= 1000 for size in args.sizes)
    ):
        parser.error("select one mode and sizes between 1 and 1000")
    inputs = args.inputs or args.out / "inputs"
    for size in args.sizes:
        root = inputs / str(size)
        if not args.measure_only:
            generate_sources(root, size)
        if args.generate_only:
            print(f"Generated {size} owned synthetic trials/session records/model spans")
            continue
        report = measure_sources(root, args.out / str(size), size, args.revision)
        _write_owned(args.out / str(size) / "measurement.json", report)
        print(
            f"Measured {size}: "
            + "; ".join(
                f"{name} import {row['import']['wall_seconds']:.3f}s, peak {row['import']['peak_tracemalloc_bytes'] / 1024 / 1024:.2f} MiB"
                for name, row in report["workloads"].items()
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
