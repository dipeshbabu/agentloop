"""Commands that analyze, compare, replay and report on traces.

Split out of ``agentloop.cli`` (issue #407). Commands register on
``agentloop.cli.app``; shared helpers are looked up through ``agentloop.cli`` at
call time, so tests and callers that patch them there keep working.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer
from rich.table import Table

from agentloop import cli
from agentloop.cli_paths import validate_output_paths
from agentloop.costs import format_cost_usd, is_cost_evaluable
from agentloop.ranking import RankingSort


@cli.app.command()
def report(path: Path) -> None:
    trace = cli._load_trace(path)
    trace.print_report()


@cli.app.command()
def compare(
    baseline: Path = Path("runs/research_agent_baseline.json"),
    optimized: Path = Path("runs/research_agent_optimized.json"),
) -> None:
    base_trace = cli._load_trace(baseline, param_hint="--baseline")
    opt_trace = cli._load_trace(optimized, param_hint="--optimized")
    from agentloop.interoperability.evidence import require_external_comparison

    try:
        require_external_comparison(base_trace, "compare")
        require_external_comparison(opt_trace, "compare")
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--baseline/--optimized") from None
    base = base_trace.report()
    opt = opt_trace.report()
    table = Table(title="AgentLoop Comparison")
    table.add_column("Metric")
    table.add_column("Baseline")
    table.add_column("Optimized")
    table.add_column("Delta")

    pairs = [
        ("Runtime sec", base["total_runtime_ms"] / 1000, opt["total_runtime_ms"] / 1000),
        ("Cost USD", base["estimated_cost_usd"], opt["estimated_cost_usd"]),
        ("Input tokens", base["input_tokens"], opt["input_tokens"]),
        ("Retry count", base["retry_count"], opt["retry_count"]),
    ]
    for name, b, o in pairs:
        if name == "Cost USD" and not (
            is_cost_evaluable(base.get("cost_status", "complete"))
            and is_cost_evaluable(opt.get("cost_status", "complete"))
        ):
            table.add_row(
                name,
                format_cost_usd(b, base.get("cost_status")),
                format_cost_usd(o, opt.get("cost_status")),
                "unavailable",
            )
            continue
        table.add_row(
            name,
            f"{b:.4f}" if isinstance(b, float) else str(b),
            f"{o:.4f}" if isinstance(o, float) else str(o),
            f"{(o - b):.4f}" if isinstance(o, float) else str(o - b),
        )
    cli.console.print(table)


@cli.app.command("replay")
def replay_command(
    baseline: Path = Path("runs/research_agent_baseline.json"),
    candidate: Path = Path("runs/research_agent_optimized.json"),
    out: Path = Path("runs/replay_report.md"),
    json_out: Path | None = typer.Option(
        None, help="Optional machine-readable replay report output."
    ),
    max_cost_regression_pct: float = typer.Option(0.0, min=0.0),
    max_latency_regression_pct: float = typer.Option(0.0, min=0.0),
    min_latency_improvement_pct: float = typer.Option(0.0, min=0.0),
    min_cost_improvement_pct: float = typer.Option(0.0, min=0.0),
    require_retry_non_increase: bool = typer.Option(True),
    require_schema_valid: bool = typer.Option(
        False, help="Require candidate trace metadata/report to mark schema output as valid."
    ),
    min_quality_score: float | None = typer.Option(
        None, min=0.0, max=1.0, help="Optional minimum candidate quality score."
    ),
    quality_fixtures: Path | None = typer.Option(None, help="Optional quality fixture JSON file."),
    fail_on_gate: bool = typer.Option(True, help="Exit non-zero when replay gates fail."),
    intervention_out: Path | None = typer.Option(
        None, help="Export linked intervention evidence, including failed gates."
    ),
    target_finding: list[str] | None = typer.Option(
        None, help="Baseline finding ID; repeat for multiple targets."
    ),
    intervention_type: str | None = typer.Option(
        None, help="Applied change, such as context_compression."
    ),
    intervention_config: Path | None = typer.Option(
        None, help="JSON object describing the applied change."
    ),
    intervention_metadata: Path | None = typer.Option(
        None, help="JSON object with task, seed, or experiment metadata."
    ),
    baseline_diagnosis: Path | None = typer.Option(
        None, help="Optional saved baseline diagnosis for historical prediction snapshots."
    ),
) -> None:
    from agentloop.interventions import (
        InterventionReferenceError,
        InterventionValidationError,
        build_intervention,
    )
    from agentloop.replay import ReplayGates, build_replay_report, replay_report_to_markdown

    validate_output_paths(
        sources=[
            baseline,
            candidate,
            quality_fixtures,
            intervention_config,
            intervention_metadata,
            baseline_diagnosis,
        ],
        outputs={"--out": out, "--json-out": json_out, "--intervention-out": intervention_out},
    )
    baseline_trace = cli._load_trace(baseline, param_hint="--baseline")
    candidate_trace = cli._load_trace(candidate, param_hint="--candidate")
    quality = (
        cli._build_quality_report_from_file(
            quality_fixtures,
            param_hint="--quality-fixtures",
            baseline_trace=baseline_trace,
            candidate_trace=candidate_trace,
            min_score=min_quality_score,
        )
        if quality_fixtures is not None
        else None
    )
    report_data = build_replay_report(
        baseline_trace,
        candidate_trace,
        gates=ReplayGates(
            max_cost_regression_pct=max_cost_regression_pct,
            max_latency_regression_pct=max_latency_regression_pct,
            min_latency_improvement_pct=min_latency_improvement_pct,
            min_cost_improvement_pct=min_cost_improvement_pct,
            require_retry_non_increase=require_retry_non_increase,
            require_schema_valid=require_schema_valid,
            min_quality_score=min_quality_score,
        ),
        quality_report=quality,
    )
    intervention = None
    if intervention_out is not None:
        try:
            intervention = build_intervention(
                baseline_trace,
                candidate_trace,
                target_finding_ids=target_finding or [],
                intervention_type=intervention_type,
                configuration=_json_object_input(intervention_config, "--intervention-config"),
                metadata=_json_object_input(intervention_metadata, "--intervention-metadata"),
                diagnosis=_json_object_input(baseline_diagnosis, "--baseline-diagnosis")
                if baseline_diagnosis
                else None,
                replay_report=report_data,
            ).to_dict()
        except (InterventionValidationError, InterventionReferenceError) as exc:
            raise typer.BadParameter(str(exc), param_hint="--intervention-out") from None
    elif any(
        (
            target_finding,
            intervention_type,
            intervention_config,
            intervention_metadata,
            baseline_diagnosis,
        )
    ):
        raise typer.BadParameter("intervention options require --intervention-out")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(replay_report_to_markdown(report_data), encoding="utf-8")
    if intervention is not None:
        cli._write_json(intervention_out, intervention)
    if json_out is not None:
        cli._write_json(json_out, report_data)
    cli.console.print(f"Wrote replay report to {out}")
    if json_out is not None:
        cli.console.print(f"Wrote replay JSON to {json_out}")
    cli.console.print(report_data["summary"])
    if fail_on_gate and not report_data["gates"]["passed"]:
        raise typer.Exit(1)


def _json_object_input(path: Path | None, param_hint: str) -> dict:
    if path is None:
        return {}
    payload = cli._read_json_input(path, param_hint=param_hint)
    if not isinstance(payload, dict):
        raise typer.BadParameter("expected a JSON object", param_hint=param_hint)
    return payload


@cli.app.command("intervention-create")
def intervention_create_command(
    request: Path,
    out: Path = typer.Option(
        Path("runs/intervention.json"), help="Export the stored evidence as JSON."
    ),
    project_id: str = typer.Option(
        "default", help="Project for a local store; remote API keys determine their project."
    ),
    api_url: str | None = typer.Option(
        None, help="Use a remote API instead of the configured local store."
    ),
    api_key: str | None = typer.Option(
        None, help="Remote project API key; defaults to AGENTLOOP_API_KEY."
    ),
) -> None:
    from agentloop.client import AgentLoopClientError
    from agentloop.intervention_service import create_stored_intervention
    from agentloop.interventions import (
        InterventionConflictError,
        InterventionReferenceError,
        InterventionValidationError,
    )
    from agentloop.quality import QualityValidationError
    from agentloop.store import get_store

    validate_output_paths(sources=[request], outputs={"--out": out})
    payload = _json_object_input(request, "request")
    try:
        record = (
            cli._remote_client(api_url, api_key).create_intervention(payload)
            if api_url
            else create_stored_intervention(get_store(), payload, project_id)
        )
    except (
        InterventionValidationError,
        InterventionReferenceError,
        InterventionConflictError,
        QualityValidationError,
        AgentLoopClientError,
    ) as exc:
        raise typer.BadParameter(str(exc), param_hint="request") from None
    cli._write_json(out, record)
    cli.console.print(f"Wrote intervention {record['intervention_id']} to {out}")


@cli.app.command("intervention-get")
def intervention_get_command(
    intervention_id: str,
    out: Path = typer.Option(Path("runs/intervention.json")),
    project_id: str = typer.Option(
        "default", help="Local project; remote API keys determine their project."
    ),
    api_url: str | None = typer.Option(None),
    api_key: str | None = typer.Option(None),
) -> None:
    from agentloop.client import AgentLoopClientError
    from agentloop.store import get_store

    try:
        record = (
            cli._remote_client(api_url, api_key).get_intervention(intervention_id)
            if api_url
            else get_store().get_intervention(intervention_id, project_id=project_id)
        )
    except AgentLoopClientError as exc:
        raise typer.BadParameter(str(exc), param_hint="intervention_id") from None
    if record is None:
        raise typer.BadParameter("intervention not found", param_hint="intervention_id")
    cli._write_json(out, record)
    cli.console.print(f"Wrote intervention to {out}")


@cli.app.command("quality-report")
def quality_report_command(
    fixtures: Path,
    baseline: Path | None = typer.Option(None, help="Optional baseline trace JSON."),
    candidate: Path | None = typer.Option(None, help="Optional candidate trace JSON."),
    out: Path = Path("runs/quality_report.md"),
    json_out: Path | None = typer.Option(
        None, help="Optional machine-readable quality report output."
    ),
    min_score: float | None = typer.Option(None, min=0.0, max=1.0),
) -> None:
    from agentloop.quality import quality_report_to_markdown

    validate_output_paths(
        sources=[fixtures, baseline, candidate], outputs={"--out": out, "--json-out": json_out}
    )
    baseline_trace = (
        cli._load_trace(baseline, param_hint="--baseline") if baseline is not None else None
    )
    candidate_trace = (
        cli._load_trace(candidate, param_hint="--candidate") if candidate is not None else None
    )
    report_data = cli._build_quality_report_from_file(
        fixtures,
        param_hint="fixtures",
        baseline_trace=baseline_trace,
        candidate_trace=candidate_trace,
        min_score=min_score,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(quality_report_to_markdown(report_data), encoding="utf-8")
    if json_out is not None:
        cli._write_json(json_out, report_data)
        cli.console.print(f"Wrote quality JSON to {json_out}")
    cli.console.print(f"Wrote quality report to {out}")
    if not report_data["passed"]:
        raise typer.Exit(1)


@cli.app.command("ci")
def ci_command(
    baseline: Path = Path("runs/research_agent_baseline.json"),
    candidate: Path = Path("runs/research_agent_optimized.json"),
    out: Path = Path("runs/agentloop_ci.md"),
    json_out: Path | None = typer.Option(None, help="Optional machine-readable CI report output."),
    max_cost_regression_pct: float = typer.Option(0.0, min=0.0),
    max_latency_regression_pct: float = typer.Option(0.0, min=0.0),
    min_latency_improvement_pct: float = typer.Option(0.0, min=0.0),
    min_cost_improvement_pct: float = typer.Option(0.0, min=0.0),
    require_retry_non_increase: bool = typer.Option(True),
    require_schema_valid: bool = typer.Option(
        False, help="Require candidate trace metadata/report to mark schema output as valid."
    ),
    min_quality_score: float | None = typer.Option(
        None, min=0.0, max=1.0, help="Optional minimum candidate quality score."
    ),
    quality_fixtures: Path | None = typer.Option(None, help="Optional quality fixture JSON file."),
    github_step_summary: bool = typer.Option(
        False, help="Append report Markdown to GITHUB_STEP_SUMMARY."
    ),
    fail_on_gate: bool = typer.Option(True, help="Exit non-zero when CI gates fail."),
) -> None:
    from agentloop.ci import build_ci_report, ci_report_to_markdown
    from agentloop.replay import ReplayGates

    summary_path = os.getenv("GITHUB_STEP_SUMMARY") if github_step_summary else None
    validate_output_paths(
        sources=[baseline, candidate, quality_fixtures],
        outputs={
            "--out": out,
            "--json-out": json_out,
            "GITHUB_STEP_SUMMARY": Path(summary_path) if summary_path else None,
        },
    )
    baseline_trace = cli._load_trace(baseline, param_hint="--baseline")
    candidate_trace = cli._load_trace(candidate, param_hint="--candidate")
    quality = (
        cli._build_quality_report_from_file(
            quality_fixtures,
            param_hint="--quality-fixtures",
            baseline_trace=baseline_trace,
            candidate_trace=candidate_trace,
            min_score=min_quality_score,
        )
        if quality_fixtures is not None
        else None
    )
    report_data = build_ci_report(
        baseline_trace,
        candidate_trace,
        gates=ReplayGates(
            max_cost_regression_pct=max_cost_regression_pct,
            max_latency_regression_pct=max_latency_regression_pct,
            min_latency_improvement_pct=min_latency_improvement_pct,
            min_cost_improvement_pct=min_cost_improvement_pct,
            require_retry_non_increase=require_retry_non_increase,
            require_schema_valid=require_schema_valid,
            min_quality_score=min_quality_score,
        ),
        quality_report=quality,
    )
    report_data["trace_inputs"]["baseline"]["path"] = str(baseline)
    report_data["trace_inputs"]["candidate"]["path"] = str(candidate)
    markdown = ci_report_to_markdown(report_data)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(markdown, encoding="utf-8")
    if json_out is not None:
        cli._write_json(json_out, report_data)
    if github_step_summary:
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as handle:
                handle.write(markdown)
    cli.console.print(f"Wrote CI report to {out}")
    if json_out is not None:
        cli.console.print(f"Wrote CI JSON to {json_out}")
    cli.console.print(report_data["replay"]["summary"])
    if fail_on_gate and not report_data["passed"]:
        raise typer.Exit(1)


@cli.app.command("dump-report")
def dump_report(path: Path, out: Path) -> None:
    validate_output_paths(sources=[path], outputs={"out": out})
    trace = cli._load_trace(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(trace.report(), indent=2), encoding="utf-8")
    cli.console.print(f"Wrote {out}")


@cli.app.command()
def audit(
    path: Path = Path("runs/research_agent_baseline.json"),
    out: Path = Path("runs/agentloop_audit.md"),
) -> None:
    from agentloop.audit import estimate_improvement
    from agentloop.exporters import export_report_markdown

    validate_output_paths(sources=[path], outputs={"--out": out})
    trace = cli._load_trace(path, param_hint="--path")
    report_data = trace.report()
    improvement = estimate_improvement(report_data)
    report_data["estimated_latency_savings_ms"] = improvement["estimated_latency_savings_ms"]
    report_data["estimated_cost_savings_usd"] = improvement["estimated_cost_savings_usd"]
    export_report_markdown(report_data, out)
    cli.console.print(f"Wrote audit to {out}")
    cli.console.print(
        f"Estimated savings: {improvement['estimated_latency_savings_ms'] / 1000:.2f}s latency, "
        f"{format_cost_usd(improvement['estimated_cost_savings_usd'])} cost per run"
    )


@cli.app.command()
def optimize(
    path: Path = Path("runs/research_agent_baseline.json"),
    out: Path = Path("runs/optimization_plan.md"),
    json_out: Path | None = typer.Option(None, help="Optional JSON output path."),
) -> None:
    from agentloop.optimizer import build_optimization_plan
    from agentloop.plan_export import export_optimization_json, export_optimization_markdown

    validate_output_paths(sources=[path], outputs={"--out": out, "--json-out": json_out})
    trace = cli._load_trace(path, param_hint="--path")
    plan = build_optimization_plan(trace)
    export_optimization_markdown(plan, out)
    if json_out is not None:
        export_optimization_json(plan, json_out)
    cli.console.print(f"Wrote optimization plan to {out}")
    if json_out is not None:
        cli.console.print(f"Wrote optimization JSON to {json_out}")
    after = plan["estimated_after"]
    cli.console.print(
        "Estimated improvement: "
        + (
            "unavailable latency, "
            if after["latency_reduction_pct"] is None
            else f"{after['latency_reduction_pct']:.1f}% latency, "
        )
        + (
            "unavailable cost"
            if after["cost_reduction_pct"] is None
            else f"{after['cost_reduction_pct']:.1f}% cost"
        )
    )


@cli.app.command("diagnose")
def diagnose(
    path: Path = Path("runs/research_agent_baseline.json"),
    out: Path = Path("runs/diagnosis.md"),
    json_out: Path | None = typer.Option(None, help="Optional machine-readable diagnosis output."),
    otel: bool = typer.Option(False, help="Read the input path as OTLP/GenAI-style JSON."),
    name: str | None = typer.Option(None, help="Trace name to use when importing OTLP JSON."),
    sort_by: RankingSort = typer.Option(
        RankingSort.PRIORITY, help="Finding ranking dimension; unknown values stay explicit."
    ),
) -> None:
    from agentloop.findings import build_diagnosis, diagnosis_to_markdown

    validate_output_paths(sources=[path], outputs={"--out": out, "--json-out": json_out})
    trace = cli._load_trace(path, otel=otel, name=name, param_hint="--path")
    diagnosis = build_diagnosis(trace, sort_by=sort_by.value)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(diagnosis_to_markdown(diagnosis), encoding="utf-8")
    if json_out is not None:
        cli._write_json(json_out, diagnosis)
    summary = diagnosis["summary"]
    cli.console.print(f"Wrote diagnosis to {out}")
    if json_out is not None:
        cli.console.print(f"Wrote diagnosis JSON to {json_out}")
    cli.console.print(
        f"Findings: {summary['finding_count']} "
        f"({summary['patchable_count']} patchable), "
        "estimated improvement: "
        + (
            "unavailable latency, "
            if summary["estimated_latency_reduction_pct"] is None
            else f"{summary['estimated_latency_reduction_pct']:.1f}% latency, "
        )
        + (
            "unavailable cost"
            if summary["estimated_cost_reduction_pct"] is None
            else f"{summary['estimated_cost_reduction_pct']:.1f}% cost"
        )
    )


@cli.app.command("import-otel")
def import_otel(
    path: Path,
    out: Path,
    name: str | None = typer.Option(None, help="Optional imported trace name."),
) -> None:
    validate_output_paths(sources=[path], outputs={"out": out})
    trace = cli._load_trace(path, otel=True, name=name)
    trace.export_json(out)
    cli.console.print(f"Imported OTLP trace {trace.run_id} to {out}")


@cli.app.command("export-otel")
def export_otel(path: Path, out: Path) -> None:
    from agentloop.otel import trace_to_otel

    validate_output_paths(sources=[path], outputs={"out": out})
    trace = cli._load_trace(path)
    cli._write_json(out, trace_to_otel(trace))
    cli.console.print(f"Exported OTLP-style trace to {out}")


@cli.app.command("patch")
def patch_command(
    path: Path = Path("runs/research_agent_baseline.json"),
    repo: Path = Path("."),
    out: Path = Path("runs/patch_plan.md"),
    json_out: Path | None = typer.Option(None, help="Optional machine-readable patch plan output."),
    otel: bool = typer.Option(False, help="Read the input path as OTLP/GenAI-style JSON."),
    name: str | None = typer.Option(None, help="Trace name to use when importing OTLP JSON."),
    dry_run: bool = typer.Option(
        True, help="Only generate a patch plan. File edits are not supported yet."
    ),
) -> None:
    from agentloop.patches import build_patch_plan, patch_plan_to_markdown

    validate_output_paths(sources=[path], outputs={"--out": out, "--json-out": json_out})
    if not dry_run:
        raise typer.BadParameter(
            "Only --dry-run is supported. Generate a patch plan first, then apply manually."
        )
    trace = cli._load_trace(path, otel=otel, name=name, param_hint="--path")
    try:
        plan = build_patch_plan(trace, repo_path=repo)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--repo") from exc
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(patch_plan_to_markdown(plan), encoding="utf-8")
    if json_out is not None:
        cli._write_json(json_out, plan)
    cli.console.print(f"Wrote patch plan to {out}")
    if json_out is not None:
        cli.console.print(f"Wrote patch plan JSON to {json_out}")
    summary = plan["summary"]
    cli.console.print(
        f"Patch plans: {summary['patch_count']}, "
        f"unsupported findings: {summary['unsupported_finding_count']}, "
        f"frameworks: {', '.join(summary['frameworks_detected']) or 'none'}"
    )


@cli.app.command("value-report")
def value_report(
    path: Path = Path("runs/research_agent_baseline.json"),
    out: Path = Path("runs/value_report.json"),
    runs_per_month: int = typer.Option(1000, min=0),
    engineer_hourly_rate_usd: float = typer.Option(150.0, min=0),
    incident_cost_usd: float = typer.Option(500.0, min=0),
) -> None:
    from agentloop.value import build_value_report

    validate_output_paths(sources=[path], outputs={"--out": out})
    trace = cli._load_trace(path, param_hint="--path")
    value = build_value_report(
        trace,
        runs_per_month=runs_per_month,
        engineer_hourly_rate_usd=engineer_hourly_rate_usd,
        incident_cost_usd=incident_cost_usd,
    )
    cli._write_json(out, value)
    cli._print_value_summary(value)
    cli.console.print(f"Wrote value report to {out}")
