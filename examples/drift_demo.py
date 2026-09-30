"""Offline synthetic regression/recovery demonstration; no deployed workload."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentloop.drift import ReviewedBaseline, compare_drift
from agentloop.drift_types import DriftRule, QualityMetric, WindowSpec
from agentloop.drift_windows import WindowBuilder
from agentloop.events import AgentEvent
from agentloop.tracer import AgentTrace
from agentloop.workflow_types import StageInfo, WorkflowInfo, operation_metadata, workflow_metadata


def make_window(day, latency, score):
    specification = WindowSpec(
        f"synthetic-window-{day}",
        "v1",
        f"2026-09-{day:02d}T00:00:00Z",
        f"2026-09-{day + 1:02d}T00:00:00Z",
        "synthetic-workload:v1",
        "fixture-config:v1",
        "fixture-model:v1",
    )
    builder = WindowBuilder(
        specification,
        quality_metrics=(QualityMetric("accuracy", "quality", "v1", "synthetic:exact-match"),),
    )
    for index in range(30):
        end = datetime(2026, 9, day, 12, tzinfo=timezone.utc)
        start = end - timedelta(milliseconds=latency)
        trace = AgentTrace(
            "synthetic-drift",
            run_id=f"synthetic-{day}-{index}",
            metadata=workflow_metadata(
                WorkflowInfo("synthetic", "v1"), status="completed", metadata={"synthetic": True}
            ),
            started_at=start.isoformat(),
            ended_at=end.isoformat(),
            elapsed_ms=latency,
        )
        metadata = operation_metadata(
            "model",
            stage=StageInfo("answer", "v1", kind="model"),
            metadata={"provider_reported_cost_usd": 0.01},
        )
        trace.add_event(
            AgentEvent(
                "event",
                trace.run_id,
                "model_call",
                "fixture-model",
                trace.started_at,
                trace.ended_at,
                latency,
                model="synthetic-model",
                input_tokens=10,
                output_tokens=2,
                token_provenance="provider",
                metadata=metadata,
            )
        )
        builder.add(trace, timeout=False, abstention=False, quality={"accuracy": score})
    return builder.to_dict()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    original = make_window(1, 100, 1)
    baseline = ReviewedBaseline(
        original,
        rules=(
            DriftRule("latency_mean_ms", relative_pct=10),
            DriftRule("quality:accuracy", "decrease", absolute=0.05),
        ),
        review_ref="synthetic-review:v1",
    )
    regression, recovery = make_window(2, 140, 0.7), make_window(3, 100, 1)
    artifacts = {
        "baseline-window": original,
        "baseline": baseline.to_dict(),
        "regression-window": regression,
        "recovery-window": recovery,
        "regression-report": compare_drift(
            baseline, regression, expected_baseline_sha256=baseline.sha256
        ),
        "recovery-report": compare_drift(
            baseline, recovery, expected_baseline_sha256=baseline.sha256
        ),
    }
    args.out_dir.mkdir(parents=True, exist_ok=False)
    for name, value in artifacts.items():
        (args.out_dir / f"{name}.json").write_text(
            json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8"
        )
    print(
        json.dumps(
            {
                "baseline_sha256": baseline.sha256,
                "regression": artifacts["regression-report"]["status"],
                "recovery": artifacts["recovery-report"]["status"],
                "synthetic": True,
            }
        )
    )


if __name__ == "__main__":
    main()
