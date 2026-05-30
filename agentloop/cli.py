from __future__ import annotations

import json
import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from agentloop.audit import estimate_improvement
from agentloop.autoinstrument import auto_instrument
from agentloop.ci import build_ci_report, ci_report_to_markdown
from agentloop.client import AgentLoopClient
from agentloop.demo import run_baseline, run_langgraph_style, run_optimized, run_proof_pair
from agentloop.doctor import run_doctor, run_production_check
from agentloop.exporters import export_report_markdown
from agentloop.findings import build_diagnosis, diagnosis_to_markdown
from agentloop.issues import build_issue_drafts, issue_drafts_to_markdown
from agentloop.optimizer import build_optimization_plan
from agentloop.otel import trace_from_otel, trace_to_otel
from agentloop.patches import build_patch_plan, patch_plan_to_markdown
from agentloop.plan_export import export_optimization_json, export_optimization_markdown
from agentloop.replay import ReplayGates, build_replay_report, replay_report_to_markdown
from agentloop.store import get_store
from agentloop.tracer import AgentTrace
from agentloop.value import build_value_report

app = typer.Typer(help="AgentLoop profiler CLI")
console = Console()


def _ensure_trace(path: Path, kind: str = "baseline") -> Path:
    if path.exists():
        return path
    console.print(f"[yellow]Trace file not found:[/yellow] {path}")
    console.print("Generating demo trace first...")
    if kind == "optimized" or "optimized" in path.name:
        return run_optimized(path.parent)
    return run_baseline(path.parent)


def _write_json(out: Path, payload: dict) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _load_trace(path: Path, *, otel: bool = False, name: str | None = None) -> AgentTrace:
    if otel:
        return trace_from_otel(json.loads(path.read_text(encoding="utf-8")), name=name)
    return AgentTrace.from_json(path)


def _print_value_summary(value: dict) -> None:
    monthly = value["monthly_value"]
    per_run = value["per_run"]
    reliability = value["reliability"]
    pricing = value.get("pricing", {})
    table = Table(title="AgentLoop Value Report")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("Total monthly value", f"${monthly['total_value_usd']:,.2f}")
    table.add_row("Model cost savings / month", f"${monthly['direct_model_cost_savings_usd']:,.2f}")
    table.add_row("Engineering hours saved / month", str(monthly["engineering_hours_saved"]))
    table.add_row("Latency saved / run", f"{per_run['latency_savings_ms'] / 1000:.3f}s")
    table.add_row("Cost saved / run", f"${per_run['cost_savings_usd']:.6f}")
    table.add_row("Reliability risk score", f"{reliability['risk_score']}/100")
    if pricing:
        table.add_row("Suggested plan", str(pricing.get("suggested_plan", "")))
        table.add_row("Suggested monthly price", f"${float(pricing.get('suggested_monthly_price_usd', 0)):,.0f}")
        ratio = pricing.get("value_to_price_ratio")
        table.add_row("Value / price ratio", "n/a" if ratio is None else f"{ratio}x")
    console.print(table)
    console.print(value["sales_summary"])


def _print_doctor(result: dict) -> None:
    table = Table(title="AgentLoop Doctor")
    table.add_column("Status")
    table.add_column("Check")
    table.add_column("Detail")
    table.add_column("Fix")
    for check in result["checks"]:
        status = check["status"]
        style = "green" if status == "ok" else "yellow" if status == "warn" else "red"
        table.add_row(f"[{style}]{status}[/{style}]", check["name"], check["detail"], check.get("fix", ""))
    console.print(table)
    if result["failed"]:
        raise typer.Exit(1)


@app.command()
def report(path: Path, autogen: bool = typer.Option(True, help="Generate a demo trace if the file is missing.")) -> None:
    if autogen:
        path = _ensure_trace(path)
    trace = AgentTrace.from_json(path)
    trace.print_report()


@app.command()
def compare(
    baseline: Path = Path("runs/research_agent_baseline.json"),
    optimized: Path = Path("runs/research_agent_optimized.json"),
    autogen: bool = typer.Option(True, help="Generate missing demo traces first."),
) -> None:
    if autogen:
        baseline = _ensure_trace(baseline, "baseline")
        optimized = _ensure_trace(optimized, "optimized")
    base = AgentTrace.from_json(baseline).report()
    opt = AgentTrace.from_json(optimized).report()
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
        table.add_row(
            name,
            f"{b:.4f}" if isinstance(b, float) else str(b),
            f"{o:.4f}" if isinstance(o, float) else str(o),
            f"{(o - b):.4f}" if isinstance(o, float) else str(o - b),
        )
    console.print(table)


@app.command("replay")
def replay_command(
    baseline: Path = Path("runs/research_agent_baseline.json"),
    candidate: Path = Path("runs/research_agent_optimized.json"),
    out: Path = Path("runs/replay_report.md"),
    json_out: Path | None = typer.Option(None, help="Optional machine-readable replay report output."),
    max_cost_regression_pct: float = typer.Option(0.0, min=0.0),
    max_latency_regression_pct: float = typer.Option(0.0, min=0.0),
    min_latency_improvement_pct: float = typer.Option(0.0, min=0.0),
    min_cost_improvement_pct: float = typer.Option(0.0, min=0.0),
    require_retry_non_increase: bool = typer.Option(True),
    require_schema_valid: bool = typer.Option(False, help="Require candidate trace metadata/report to mark schema output as valid."),
    min_quality_score: float | None = typer.Option(None, min=0.0, help="Optional minimum candidate quality score."),
    fail_on_gate: bool = typer.Option(True, help="Exit non-zero when replay gates fail."),
    autogen: bool = typer.Option(True, help="Generate missing demo traces first."),
) -> None:
    if autogen:
        baseline = _ensure_trace(baseline, "baseline")
        candidate = _ensure_trace(candidate, "optimized")
    baseline_trace = AgentTrace.from_json(baseline)
    candidate_trace = AgentTrace.from_json(candidate)
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
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(replay_report_to_markdown(report_data), encoding="utf-8")
    if json_out is not None:
        _write_json(json_out, report_data)
    console.print(f"Wrote replay report to {out}")
    if json_out is not None:
        console.print(f"Wrote replay JSON to {json_out}")
    console.print(report_data["summary"])
    if fail_on_gate and not report_data["gates"]["passed"]:
        raise typer.Exit(1)


@app.command("ci")
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
    require_schema_valid: bool = typer.Option(False, help="Require candidate trace metadata/report to mark schema output as valid."),
    min_quality_score: float | None = typer.Option(None, min=0.0, help="Optional minimum candidate quality score."),
    github_step_summary: bool = typer.Option(False, help="Append report Markdown to GITHUB_STEP_SUMMARY."),
    fail_on_gate: bool = typer.Option(True, help="Exit non-zero when CI gates fail."),
    autogen: bool = typer.Option(True, help="Generate missing demo traces first."),
) -> None:
    if autogen:
        baseline = _ensure_trace(baseline, "baseline")
        candidate = _ensure_trace(candidate, "optimized")
    report_data = build_ci_report(
        AgentTrace.from_json(baseline),
        AgentTrace.from_json(candidate),
        gates=ReplayGates(
            max_cost_regression_pct=max_cost_regression_pct,
            max_latency_regression_pct=max_latency_regression_pct,
            min_latency_improvement_pct=min_latency_improvement_pct,
            min_cost_improvement_pct=min_cost_improvement_pct,
            require_retry_non_increase=require_retry_non_increase,
            require_schema_valid=require_schema_valid,
            min_quality_score=min_quality_score,
        ),
    )
    markdown = ci_report_to_markdown(report_data)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(markdown, encoding="utf-8")
    if json_out is not None:
        _write_json(json_out, report_data)
    if github_step_summary:
        summary_path = os.getenv("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as handle:
                handle.write(markdown)
    console.print(f"Wrote CI report to {out}")
    if json_out is not None:
        console.print(f"Wrote CI JSON to {json_out}")
    console.print(report_data["replay"]["summary"])
    if fail_on_gate and not report_data["passed"]:
        raise typer.Exit(1)


@app.command("dump-report")
def dump_report(path: Path, out: Path, autogen: bool = typer.Option(True)) -> None:
    if autogen:
        path = _ensure_trace(path)
    trace = AgentTrace.from_json(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(trace.report(), indent=2), encoding="utf-8")
    console.print(f"Wrote {out}")


@app.command()
def audit(
    path: Path = Path("runs/research_agent_baseline.json"),
    out: Path = Path("runs/agentloop_audit.md"),
    autogen: bool = typer.Option(True),
) -> None:
    if autogen:
        path = _ensure_trace(path)
    trace = AgentTrace.from_json(path)
    report_data = trace.report()
    improvement = estimate_improvement(report_data)
    report_data["estimated_latency_savings_ms"] = improvement["estimated_latency_savings_ms"]
    report_data["estimated_cost_savings_usd"] = improvement["estimated_cost_savings_usd"]
    export_report_markdown(report_data, out)
    console.print(f"Wrote audit to {out}")
    console.print(
        f"Estimated savings: {improvement['estimated_latency_savings_ms'] / 1000:.2f}s latency, "
        f"${improvement['estimated_cost_savings_usd']:.4f} cost per run"
    )


@app.command()
def optimize(
    path: Path = Path("runs/research_agent_baseline.json"),
    out: Path = Path("runs/optimization_plan.md"),
    json_out: Path | None = typer.Option(None, help="Optional JSON output path."),
    autogen: bool = typer.Option(True, help="Generate a demo trace if the file is missing."),
) -> None:
    if autogen:
        path = _ensure_trace(path)
    trace = AgentTrace.from_json(path)
    plan = build_optimization_plan(trace)
    export_optimization_markdown(plan, out)
    if json_out is not None:
        export_optimization_json(plan, json_out)
    console.print(f"Wrote optimization plan to {out}")
    if json_out is not None:
        console.print(f"Wrote optimization JSON to {json_out}")
    after = plan["estimated_after"]
    console.print(
        f"Estimated improvement: {after['latency_reduction_pct']:.1f}% latency, "
        f"{after['cost_reduction_pct']:.1f}% cost"
    )


@app.command("diagnose")
def diagnose(
    path: Path = Path("runs/research_agent_baseline.json"),
    out: Path = Path("runs/diagnosis.md"),
    json_out: Path | None = typer.Option(None, help="Optional machine-readable diagnosis output."),
    otel: bool = typer.Option(False, help="Read the input path as OTLP/GenAI-style JSON."),
    name: str | None = typer.Option(None, help="Trace name to use when importing OTLP JSON."),
    autogen: bool = typer.Option(True, help="Generate a demo trace if the native trace file is missing."),
) -> None:
    if autogen and not otel:
        path = _ensure_trace(path)
    trace = _load_trace(path, otel=otel, name=name)
    diagnosis = build_diagnosis(trace)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(diagnosis_to_markdown(diagnosis), encoding="utf-8")
    if json_out is not None:
        _write_json(json_out, diagnosis)
    summary = diagnosis["summary"]
    console.print(f"Wrote diagnosis to {out}")
    if json_out is not None:
        console.print(f"Wrote diagnosis JSON to {json_out}")
    console.print(
        f"Findings: {summary['finding_count']} "
        f"({summary['patchable_count']} patchable), "
        f"estimated improvement: {summary['estimated_latency_reduction_pct']:.1f}% latency, "
        f"{summary['estimated_cost_reduction_pct']:.1f}% cost"
    )


@app.command("import-otel")
def import_otel(path: Path, out: Path, name: str | None = typer.Option(None, help="Optional imported trace name.")) -> None:
    trace = trace_from_otel(json.loads(path.read_text(encoding="utf-8")), name=name)
    trace.export_json(out)
    console.print(f"Imported OTLP trace {trace.run_id} to {out}")


@app.command("export-otel")
def export_otel(path: Path, out: Path, autogen: bool = typer.Option(True)) -> None:
    if autogen:
        path = _ensure_trace(path)
    trace = AgentTrace.from_json(path)
    _write_json(out, trace_to_otel(trace))
    console.print(f"Exported OTLP-style trace to {out}")


@app.command("patch")
def patch_command(
    path: Path = Path("runs/research_agent_baseline.json"),
    repo: Path = Path("."),
    out: Path = Path("runs/patch_plan.md"),
    json_out: Path | None = typer.Option(None, help="Optional machine-readable patch plan output."),
    otel: bool = typer.Option(False, help="Read the input path as OTLP/GenAI-style JSON."),
    name: str | None = typer.Option(None, help="Trace name to use when importing OTLP JSON."),
    dry_run: bool = typer.Option(True, help="Only generate a patch plan. File edits are not supported yet."),
    autogen: bool = typer.Option(True, help="Generate a demo trace if the native trace file is missing."),
) -> None:
    if not dry_run:
        raise typer.BadParameter("Only --dry-run is supported. Generate a patch plan first, then apply manually.")
    if autogen and not otel:
        path = _ensure_trace(path)
    trace = _load_trace(path, otel=otel, name=name)
    plan = build_patch_plan(trace, repo_path=repo)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(patch_plan_to_markdown(plan), encoding="utf-8")
    if json_out is not None:
        _write_json(json_out, plan)
    console.print(f"Wrote patch plan to {out}")
    if json_out is not None:
        console.print(f"Wrote patch plan JSON to {json_out}")
    summary = plan["summary"]
    console.print(
        f"Patch plans: {summary['patch_count']}, "
        f"unsupported findings: {summary['unsupported_finding_count']}, "
        f"frameworks: {', '.join(summary['frameworks_detected']) or 'none'}"
    )


@app.command("value-report")
def value_report(
    path: Path = Path("runs/research_agent_baseline.json"),
    out: Path = Path("runs/value_report.json"),
    runs_per_month: int = typer.Option(1000, min=0),
    engineer_hourly_rate_usd: float = typer.Option(150.0, min=0),
    incident_cost_usd: float = typer.Option(500.0, min=0),
    autogen: bool = typer.Option(True),
) -> None:
    if autogen:
        path = _ensure_trace(path)
    trace = AgentTrace.from_json(path)
    value = build_value_report(
        trace,
        runs_per_month=runs_per_month,
        engineer_hourly_rate_usd=engineer_hourly_rate_usd,
        incident_cost_usd=incident_cost_usd,
    )
    _write_json(out, value)
    _print_value_summary(value)
    console.print(f"Wrote value report to {out}")


@app.command("doctor")
def doctor_command(
    check_api: bool = typer.Option(True, help="Also call the configured /health endpoint."),
    json_out: Path | None = typer.Option(None, help="Optional path for machine-readable doctor output."),
) -> None:
    result = run_doctor(check_api=check_api)
    if json_out:
        _write_json(json_out, result)
        console.print(f"Wrote doctor output to {json_out}")
    _print_doctor(result)


@app.command("production-check")
def production_check_command(
    check_api: bool = typer.Option(True, help="Call the configured /health and /readyz endpoints."),
    check_store: bool = typer.Option(True, help="Initialize and query the configured store."),
    allow_http: bool = typer.Option(False, help="Allow non-HTTPS AGENTLOOP_API_URL for local staging."),
    json_out: Path | None = typer.Option(None, help="Optional path for machine-readable check output."),
) -> None:
    result = run_production_check(check_api=check_api, check_store=check_store, allow_http=allow_http)
    if json_out:
        _write_json(json_out, result)
        console.print(f"Wrote production check output to {json_out}")
    _print_doctor(result)


@app.command("auto-instrument")
def auto_instrument_command(json_out: Path | None = typer.Option(None)) -> None:
    result = auto_instrument().to_dict()
    if json_out:
        _write_json(json_out, result)
        console.print(f"Wrote auto instrumentation output to {json_out}")
    console.print_json(data=result)


@app.command("init-store")
def init_store() -> None:
    db = get_store()
    db.init()
    console.print("Initialized AgentLoop store")


@app.command("create-api-key")
def create_api_key(project_id: str = "default", name: str = "default") -> None:
    db = get_store()
    key = db.create_api_key(project_id=project_id, name=name)
    console.print("Created API key. Save it now; it will not be shown again.")
    console.print(key["api_key"])


@app.command("store-trace")
def store_trace(
    path: Path = Path("runs/research_agent_baseline.json"),
    project_id: str = "default",
    autogen: bool = typer.Option(True),
) -> None:
    if autogen:
        path = _ensure_trace(path)
    trace = AgentTrace.from_json(path)
    db = get_store()
    db.save_trace(trace, project_id=project_id)
    console.print(f"Stored trace {trace.run_id} under project {project_id}")


@app.command("list-stored-traces")
def list_stored_traces(project_id: str | None = None) -> None:
    db = get_store()
    traces = db.list_traces(project_id=project_id)
    table = Table(title="Stored AgentLoop Traces")
    table.add_column("Project")
    table.add_column("Run ID")
    table.add_column("Name")
    table.add_column("Runtime ms")
    table.add_column("Cost")
    for item in traces:
        table.add_row(
            str(item.get("project_id", "")),
            str(item.get("run_id", "")),
            str(item.get("name", "")),
            f"{float(item.get('total_runtime_ms', 0)):.2f}",
            f"${float(item.get('estimated_cost_usd', 0)):.4f}",
        )
    console.print(table)


@app.command("list-findings")
def list_findings(project_id: str | None = None, status: str | None = None) -> None:
    db = get_store()
    findings = db.list_findings(project_id=project_id, status=status)
    table = Table(title="AgentLoop Findings")
    table.add_column("Severity")
    table.add_column("Type")
    table.add_column("Title")
    table.add_column("Run")
    table.add_column("Status")
    table.add_column("Patchable")
    table.add_column("Savings")
    for finding in findings:
        table.add_row(
            str(finding["severity"]),
            str(finding["type"]),
            str(finding["title"]),
            str(finding["run_id"]),
            str(finding["status"]),
            "yes" if finding["patchable"] else "no",
            f"{finding['estimated_latency_savings_ms'] / 1000:.2f}s / ${finding['estimated_cost_savings_usd']:.4f}",
        )
    console.print(table)


@app.command("optimization-queue")
def optimization_queue(project_id: str | None = None, json_out: Path | None = typer.Option(None)) -> None:
    db = get_store()
    queue = db.optimization_queue(project_id=project_id)
    payload = {"project_id": project_id, "queue": queue}
    if json_out is not None:
        _write_json(json_out, payload)
        console.print(f"Wrote optimization queue JSON to {json_out}")
    table = Table(title="AgentLoop Optimization Queue")
    table.add_column("Priority")
    table.add_column("Severity")
    table.add_column("Type")
    table.add_column("Title")
    table.add_column("Runs")
    table.add_column("Patchable")
    table.add_column("Savings")
    for item in queue:
        table.add_row(
            f"{item['priority_score']:.1f}",
            str(item["severity"]),
            str(item["type"]),
            str(item["title"]),
            str(item["run_count"]),
            str(item["patchable_count"]),
            f"{item['estimated_latency_savings_ms'] / 1000:.2f}s / ${item['estimated_cost_savings_usd']:.4f}",
        )
    console.print(table)


@app.command("github-issue-drafts")
def github_issue_drafts(
    project_id: str | None = None,
    out: Path = Path("runs/agentloop_issue_drafts.md"),
    json_out: Path | None = typer.Option(None),
    limit: int = typer.Option(5, min=1, max=20),
) -> None:
    db = get_store()
    drafts = build_issue_drafts(db.optimization_queue(project_id=project_id), limit=limit)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(issue_drafts_to_markdown(drafts), encoding="utf-8")
    if json_out is not None:
        _write_json(json_out, {"project_id": project_id, "issue_drafts": drafts})
        console.print(f"Wrote GitHub issue draft JSON to {json_out}")
    console.print(f"Wrote GitHub issue drafts to {out}")


@app.command("usage-summary")
def usage_summary(project_id: str | None = None) -> None:
    db = get_store()
    summary = db.usage_summary(project_id=project_id)
    table = Table(title="AgentLoop Usage Summary")
    table.add_column("Metric")
    table.add_column("Value")
    for key, value in summary.items():
        table.add_row(key, str(value))
    console.print(table)


@app.command()
def upload(
    path: Path = Path("runs/research_agent_baseline.json"),
    api_url: str = typer.Option("http://127.0.0.1:8000", help="AgentLoop API base URL."),
    api_key: str | None = typer.Option(None, help="Optional API key. Defaults to AGENTLOOP_API_KEY."),
    autogen: bool = typer.Option(True, help="Generate a demo trace if the file is missing."),
) -> None:
    if autogen:
        path = _ensure_trace(path)
    client = AgentLoopClient(base_url=api_url, api_key=api_key)
    response = client.upload_trace(path)
    console.print(f"Uploaded trace {response['run_id']} to {api_url}")
    console.print(f"Project: {response.get('project_id', 'default')}")


@app.command("remote-optimize")
def remote_optimize(
    run_id: str,
    api_url: str = typer.Option("http://127.0.0.1:8000", help="AgentLoop API base URL."),
    api_key: str | None = typer.Option(None, help="Optional API key. Defaults to AGENTLOOP_API_KEY."),
    out: Path = Path("runs/remote_optimization_plan.json"),
) -> None:
    client = AgentLoopClient(base_url=api_url, api_key=api_key)
    plan = client.get_optimization_plan(run_id)
    export_optimization_json(plan, out)
    console.print(f"Wrote remote optimization plan to {out}")


@app.command("remote-diagnose")
def remote_diagnose(
    run_id: str,
    api_url: str = typer.Option("http://127.0.0.1:8000", help="AgentLoop API base URL."),
    api_key: str | None = typer.Option(None, help="Optional API key. Defaults to AGENTLOOP_API_KEY."),
    out: Path = Path("runs/remote_diagnosis.json"),
) -> None:
    client = AgentLoopClient(base_url=api_url, api_key=api_key)
    diagnosis = client.get_diagnosis(run_id)
    _write_json(out, diagnosis)
    console.print(f"Wrote remote diagnosis to {out}")


@app.command("remote-findings")
def remote_findings(
    api_url: str = typer.Option("http://127.0.0.1:8000", help="AgentLoop API base URL."),
    api_key: str | None = typer.Option(None, help="Optional API key. Defaults to AGENTLOOP_API_KEY."),
    project_id: str | None = typer.Option(None),
    status: str | None = typer.Option(None),
    out: Path = Path("runs/remote_findings.json"),
) -> None:
    client = AgentLoopClient(base_url=api_url, api_key=api_key)
    findings = client.list_findings(project_id=project_id, status=status)
    _write_json(out, findings)
    console.print(f"Wrote remote findings to {out}")


@app.command("remote-optimization-queue")
def remote_optimization_queue(
    api_url: str = typer.Option("http://127.0.0.1:8000", help="AgentLoop API base URL."),
    api_key: str | None = typer.Option(None, help="Optional API key. Defaults to AGENTLOOP_API_KEY."),
    project_id: str | None = typer.Option(None),
    out: Path = Path("runs/remote_optimization_queue.json"),
) -> None:
    client = AgentLoopClient(base_url=api_url, api_key=api_key)
    queue = client.optimization_queue(project_id=project_id)
    _write_json(out, queue)
    console.print(f"Wrote remote optimization queue to {out}")


@app.command("remote-github-issue-drafts")
def remote_github_issue_drafts(
    api_url: str = typer.Option("http://127.0.0.1:8000", help="AgentLoop API base URL."),
    api_key: str | None = typer.Option(None, help="Optional API key. Defaults to AGENTLOOP_API_KEY."),
    project_id: str | None = typer.Option(None),
    limit: int = typer.Option(5, min=1, max=20),
    out: Path = Path("runs/remote_agentloop_issue_drafts.json"),
) -> None:
    client = AgentLoopClient(base_url=api_url, api_key=api_key)
    drafts = client.github_issue_drafts(project_id=project_id, limit=limit)
    _write_json(out, drafts)
    console.print(f"Wrote remote GitHub issue drafts to {out}")


@app.command("remote-value-report")
def remote_value_report(
    run_id: str,
    api_url: str = typer.Option("http://127.0.0.1:8000", help="AgentLoop API base URL."),
    api_key: str | None = typer.Option(None, help="Optional API key. Defaults to AGENTLOOP_API_KEY."),
    out: Path = Path("runs/remote_value_report.json"),
    runs_per_month: int = typer.Option(1000, min=0),
    engineer_hourly_rate_usd: float = typer.Option(150.0, min=0),
    incident_cost_usd: float = typer.Option(500.0, min=0),
) -> None:
    client = AgentLoopClient(base_url=api_url, api_key=api_key)
    value = client.get_value_report(
        run_id,
        runs_per_month=runs_per_month,
        engineer_hourly_rate_usd=engineer_hourly_rate_usd,
        incident_cost_usd=incident_cost_usd,
    )
    _write_json(out, value)
    _print_value_summary(value)
    console.print(f"Wrote remote value report to {out}")


@app.command("remote-usage")
def remote_usage(
    api_url: str = typer.Option("http://127.0.0.1:8000", help="AgentLoop API base URL."),
    api_key: str | None = typer.Option(None, help="Optional API key. Defaults to AGENTLOOP_API_KEY."),
) -> None:
    client = AgentLoopClient(base_url=api_url, api_key=api_key)
    console.print_json(data=client.usage_summary())


@app.command("remote-create-api-key")
def remote_create_api_key(
    project_id: str = "default",
    name: str = "default",
    api_url: str = typer.Option("http://127.0.0.1:8000", help="AgentLoop API base URL."),
    admin_api_key: str | None = typer.Option(None, help="Admin API key. Defaults to AGENTLOOP_ADMIN_API_KEY."),
) -> None:
    client = AgentLoopClient(base_url=api_url, admin_api_key=admin_api_key)
    key = client.create_api_key(project_id=project_id, name=name)
    console.print("Created hosted API key. Save it now; it will not be shown again.")
    console.print(key["api_key"])


@app.command()
def demo(kind: str = typer.Option("baseline", help="baseline, optimized, langgraph, or proof")) -> None:
    if kind == "baseline":
        path = run_baseline()
    elif kind == "optimized":
        path = run_optimized()
    elif kind == "langgraph":
        path = run_langgraph_style()
    elif kind == "proof":
        baseline, candidate = run_proof_pair()
        console.print(f"Wrote trace to {baseline}")
        console.print(f"Wrote trace to {candidate}")
        return
    else:
        raise typer.BadParameter("kind must be 'baseline', 'optimized', 'langgraph', or 'proof'")
    console.print(f"Wrote trace to {path}")


@app.command("demo-all")
def demo_all() -> None:
    base = run_baseline()
    opt = run_optimized()
    lg = run_langgraph_style()
    proof_base, proof_candidate = run_proof_pair()
    console.print(f"Wrote {base}")
    console.print(f"Wrote {opt}")
    console.print(f"Wrote {lg}")
    console.print(f"Wrote {proof_base}")
    console.print(f"Wrote {proof_candidate}")


@app.command()
def server(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    try:
        import uvicorn
    except ImportError as exc:
        raise typer.BadParameter("Install server dependencies with: pip install -e '.[server]'") from exc
    uvicorn.run("agentloop.server:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    app()
