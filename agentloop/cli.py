from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

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
        table.add_row(name, f"{b:.4f}" if isinstance(b, float) else str(b), f"{o:.4f}" if isinstance(o, float) else str(o), f"{(o - b):.4f}" if isinstance(o, float) else str(o - b))
    console.print(table)


@app.command()
def dump_report(path: Path, out: Path) -> None:
    trace = AgentTrace.from_json(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(trace.report(), indent=2), encoding="utf-8")
    console.print(f"Wrote {out}")
