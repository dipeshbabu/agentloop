"""Local window collection, explicit baseline review references and drift reports."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from agentloop.aggregate_stats import AggregateConfig
from agentloop.aggregates import TraceAggregate, read_json
from agentloop.drift import ReviewedBaseline, compare_drift
from agentloop.drift_types import DriftRule, QualityMetric, WindowSpec
from agentloop.drift_windows import WindowBuilder
from agentloop.tracer import AgentTrace

drift_app = typer.Typer(help="Compare local evidence windows against immutable reviewed baselines.")


def collect_window(path):
    path = Path(path)
    manifest = read_json(path)
    if manifest.get("schema_version") != "1.0":
        raise ValueError("unsupported window manifest")
    raw_config = manifest.get("aggregate_config", {})
    config = AggregateConfig(
        **{
            key: tuple(value) if key == "latency_bounds_ms" else value
            for key, value in raw_config.items()
        }
    )
    builder = WindowBuilder(
        WindowSpec(**manifest["spec"]),
        quality_metrics=tuple(
            QualityMetric(**item) for item in manifest.get("quality_metrics", [])
        ),
        config=config,
        include_findings=manifest.get("include_findings", False),
    )
    records = manifest["records"]
    if not isinstance(records, list) or len(records) > 100_000:
        raise ValueError("manifest records must be a list of at most 100000 entries")
    for record in records:
        if (
            not isinstance(record, dict)
            or not set(record)
            <= {
                "trace",
                "aggregate",
                "cohort",
                "timeout",
                "abstention",
                "quality",
                "time_assignment_ref",
            }
            or ("trace" in record) == ("aggregate" in record)
        ):
            raise ValueError("each record needs exactly one trace or aggregate path")
        cohort = record.get("cohort", "all")
        if "trace" in record:
            if "time_assignment_ref" in record:
                raise ValueError("native traces use their own completion timestamps")
            builder.add(
                AgentTrace.from_dict(read_json(path.parent / record["trace"])),
                cohort=cohort,
                timeout=record.get("timeout"),
                abstention=record.get("abstention"),
                quality=record.get("quality"),
            )
        else:
            if set(record) & {"timeout", "abstention", "quality"}:
                raise ValueError("per-run annotations cannot be broadcast over an aggregate")
            builder.add_aggregate(
                TraceAggregate.from_dict(read_json(path.parent / record["aggregate"])),
                cohort=cohort,
                time_assignment_ref=record.get("time_assignment_ref"),
            )
    return builder.to_dict()


def _write_new(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, allow_nan=False)
        handle.write("\n")


@drift_app.command("collect")
def collect_command(manifest: Path, out: Path = typer.Option(...)):
    """Create a content-bound window from disjoint trace/aggregate records."""
    from agentloop.cli import console

    try:
        result = collect_window(manifest)
        _write_new(out, result)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise typer.BadParameter(str(exc), param_hint="manifest/out") from None
    console.print(f"Wrote window {result['sha256']} to {out}", markup=False)


@drift_app.command("baseline")
def baseline_command(
    window: Path, rules: Path, review_ref: str = typer.Option(...), out: Path = typer.Option(...)
):
    """Freeze an explicit host-reviewed window and its thresholds; never overwrite."""
    from agentloop.cli import console

    try:
        definitions = read_json(rules)
        if not isinstance(definitions, list) or len(definitions) > 32:
            raise ValueError("rules must contain at most 32 declarations")
        result = ReviewedBaseline(
            read_json(window),
            rules=tuple(DriftRule(**item) for item in definitions),
            review_ref=review_ref,
        )
        _write_new(out, result.to_dict())
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise typer.BadParameter(str(exc), param_hint="window/rules/out") from None
    console.print(f"Reviewed baseline SHA256: {result.sha256}", markup=False)


@drift_app.command("compare")
def compare_command(
    baseline: Path,
    current: Path,
    baseline_sha256: str = typer.Option(..., help="Pin the reviewed baseline content hash."),
    out: Path = typer.Option(...),
):
    """Write descriptive drift evidence; take no notification or deployment action."""
    from agentloop.cli import console

    try:
        result = compare_drift(
            ReviewedBaseline.from_dict(read_json(baseline)),
            read_json(current),
            expected_baseline_sha256=baseline_sha256,
        )
        _write_new(out, result)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise typer.BadParameter(str(exc), param_hint="baseline/current/out") from None
    console.print(f"Drift status: {result['status']}", markup=False)
    for metric in result["metrics"]:
        console.print(
            f"{metric['cohort']} / {metric['metric']}: {metric['status']} ({metric['reason']})",
            markup=False,
        )
