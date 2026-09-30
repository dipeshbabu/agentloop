"""Offline adapter/import overhead measurements, never a provider latency claim."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from agentloop import instrument, reset_runtime, trace_agent
from agentloop.onboarding import onboard
from agentloop.otel import traces_from_otel

ROOT = Path(__file__).resolve().parents[1]


def _summary(values):
    ordered = sorted(values)
    return {
        "count": len(values),
        "median_ms": statistics.median(values),
        "min_ms": min(values),
        "max_ms": max(values),
        "p95_observed_ms": ordered[max(0, (95 * len(values) + 99) // 100 - 1)],
    }


def benchmark(*, repetitions=20, calls=100):
    if (
        type(repetitions) is not int
        or not 3 <= repetitions <= 1000
        or type(calls) is not int
        or not 1 <= calls <= 1000
    ):
        raise ValueError("repetitions must be 3..1000 and calls 1..1000")
    response = {"usage": {"input_tokens": 10, "output_tokens": 2}}

    def create(**kwargs):
        return response

    client = instrument(
        SimpleNamespace(responses=SimpleNamespace(create=create)),
        integration="openai",
        enabled=True,
    )
    fixture_path = ROOT / "tests/fixtures/telemetry/openinference.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    payload = fixture["payload"]
    rows = []
    for repeat in range(repetitions + 2):
        measurements = {}
        # Rotate conditions to reduce a fixed ordering bias; two warm-up rounds.
        conditions = ["baseline", "installed_without_trace", "active_trace", "otlp_import"]
        shift = repeat % len(conditions)
        for condition in conditions[shift:] + conditions[:shift]:
            started = time.perf_counter_ns()
            if condition == "active_trace":
                with trace_agent("synthetic_onboarding_benchmark") as captured:
                    fn = client.responses.create
                    for _ in range(calls):
                        if fn(model="offline-fixture") is not response:
                            raise RuntimeError("adapter changed result identity")
                if len(captured.events) != calls:
                    raise RuntimeError("adapter did not record exactly one event per call")
            elif condition == "otlp_import":
                for _ in range(calls):
                    imported = traces_from_otel(payload)
                if not imported or not imported[0].events:
                    raise RuntimeError("standards import captured no execution")
            else:
                fn = create if condition == "baseline" else client.responses.create
                for _ in range(calls):
                    if fn(model="offline-fixture") is not response:
                        raise RuntimeError("adapter changed result identity")
            measurements[condition] = (time.perf_counter_ns() - started) / 1_000_000 / calls
        if repeat >= 2:
            rows.append(
                {
                    "repeat": repeat - 2,
                    **measurements,
                    "active_increment_ms_per_call": measurements["active_trace"]
                    - measurements["baseline"],
                    "inactive_increment_ms_per_call": measurements["installed_without_trace"]
                    - measurements["baseline"],
                }
            )
    files = [
        "examples/onboarding_benchmark.py",
        "agentloop/autoinstrument.py",
        "agentloop/integrations/openai.py",
        "agentloop/otel.py",
        "agentloop/otel_semantics.py",
        "agentloop/tracer.py",
        "agentloop/onboarding.py",
    ]
    return {
        "schema_version": "1.0",
        "synthetic": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "environment": {"python": sys.version.split()[0], "platform": platform.platform()},
        "protocol": {
            "repetitions": repetitions,
            "calls_per_condition": calls,
            "warmup_rounds": 2,
            "order": "rotated condition order",
            "unit": "ms per local call or parsed OTLP payload import",
            "active_trace_scope": "host boundary and event recording included; no store/upload/report rendering",
            "baseline": "same local function returning a fixed synthetic SDK-shaped usage response",
        },
        "source_hash_encoding": "UTF-8 with universal newlines normalized to LF",
        "source_sha256": {
            name: hashlib.sha256(
                (ROOT / name).read_text(encoding="utf-8").encode("utf-8")
            ).hexdigest()
            for name in files
        },
        "fixture_sha256": hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
        "setup_steps": {
            "sdk": {
                "count": 3,
                "steps": [
                    "install AgentLoop",
                    "instrument the existing client at startup",
                    "wrap the host execution in one trace boundary",
                ],
            },
            "existing_otlp": {
                "count": 2,
                "steps": [
                    "export existing telemetry to OTLP JSON",
                    "run agentloop onboard with format otlp",
                ],
            },
        },
        "rows": rows,
        "summaries": {
            key: _summary([row[key] for row in rows]) for key in rows[0] if key != "repeat"
        },
        "import_validation": onboard(payload, format="otlp", analyze=False),
        "limitations": [
            "Local synthetic callback overhead; no provider/network/SDK service latency.",
            "OTLP timing starts from parsed JSON; file IO and exporter overhead are excluded.",
            "Descriptive single-machine repetitions, not confidence intervals or production overhead guarantees.",
            "No paid calls; setup steps exclude application-specific exporter configuration and task-quality validation.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=20)
    parser.add_argument("--calls", type=int, default=100)
    args = parser.parse_args()
    # Keep the standalone measurement offline even when the host has auto-export configured.
    reset_runtime()
    result = benchmark(repetitions=args.repetitions, calls=args.calls)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, allow_nan=False)
        handle.write("\n")
    print(json.dumps({"out": str(args.out), "summaries": result["summaries"]}))


if __name__ == "__main__":
    main()
