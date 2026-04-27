from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from agentloop.audit import estimate_improvement
from agentloop.demo import run_baseline, run_langgraph_style, run_optimized
from agentloop.exporters import export_report_markdown
from agentloop.optimizer import build_optimization_plan
from agentloop.plan_export import export_optimization_json, export_optimization_markdown
from agentloop.tracer import AgentTrace

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


@app.command()
def demo(kind: str = typer.Option("baseline", help="baseline, optimized, or langgraph")) -> None:
    if kind == "baseline":
        path = run_baseline()
    elif kind == "optimized":
        path = run_optimized()
    elif kind == "langgraph":
        path = run_langgraph_style()
    else:
        raise typer.BadParameter("kind must be 'baseline', 'optimized', or 'langgraph'")
    console.print(f"Wrote trace to {path}")


@app.command("demo-all")
def demo_all() -> None:
    base = run_baseline()
    opt = run_optimized()
    lg = run_langgraph_style()
    console.print(f"Wrote {base}")
    console.print(f"Wrote {opt}")
    console.print(f"Wrote {lg}")


@app.command()
def server(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    try:
        import uvicorn
    except ImportError as exc:
        raise typer.BadParameter("Install server dependencies with: pip install -e '.[server]'") from exc
    uvicorn.run("agentloop.server:app", host=host, port=port, reload=reload)
