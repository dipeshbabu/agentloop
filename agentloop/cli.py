from __future__ import annotations

import importlib
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from agentloop.audit import estimate_improvement
from agentloop.exporters import export_report_markdown
from agentloop.tracer import AgentTrace

app = typer.Typer(help="AgentLoop profiler CLI")
console = Console()


@app.command()
def report(path: Path) -> None:
    trace = AgentTrace.from_json(path)
    trace.print_report()


@app.command()
def compare(baseline: Path, optimized: Path) -> None:
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
def dump_report(path: Path, out: Path) -> None:
    trace = AgentTrace.from_json(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(trace.report(), indent=2), encoding="utf-8")
    console.print(f"Wrote {out}")


@app.command()
def audit(path: Path, out: Path = Path("runs/agentloop_audit.md")) -> None:
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
def demo(kind: str = typer.Option("baseline", help="baseline or optimized")) -> None:
    if kind not in {"baseline", "optimized"}:
        raise typer.BadParameter("kind must be 'baseline' or 'optimized'")
    module_name = "examples.research_agent_demo" if kind == "baseline" else "examples.optimized_research_agent_demo"
    module = importlib.import_module(module_name)
    module.main()
