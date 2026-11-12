"""Guided command-line entry points for user-owned agent workflows."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from agentloop import improvement

improve_app = typer.Typer(
    help="Capture fixed tasks, review baseline findings, and verify an applied change."
)
console = Console()


@improve_app.command("capture")
def capture_command(
    tasks: Path = typer.Option(..., help="Fixed task inputs and deterministic quality checks."),
    out: Path = typer.Option(..., help="Fresh output directory for this condition."),
    timeout: float = typer.Option(180, min=0.01, max=3600, help="Seconds per task attempt."),
    show_runner_output: bool = typer.Option(
        False, help="Stream the runner's stdout and stderr for diagnosis."
    ),
    command: list[str] = typer.Argument(
        ..., help="Explicit runner argv after --. Receives a JSON request on stdin."
    ),
):
    """Run every declared task and retain failed, missing and interrupted attempts."""
    try:
        manifest = improvement.capture(
            tasks, out, command, timeout=timeout, show_output=show_runner_output
        )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    complete = sum(row["status"] == "completed" for row in manifest["runs"])
    console.print(f"Captured {complete} of {len(manifest['runs'])} planned attempts.", markup=False)
    console.print(f"Capture manifest written to {out / 'capture.json'}", markup=False)
    if complete != len(manifest["runs"]):
        raise typer.Exit(1)


@improve_app.command("start")
def start_command(
    tasks: Path = typer.Option(...),
    baseline: Path = typer.Option(..., help="Baseline capture.json."),
    out: Path = typer.Option(..., help="Fresh session directory."),
    focus: str | None = typer.Option(
        None, help="Finding type to investigate. Defaults to the first baseline finding."
    ),
    objective: str = typer.Option("latency", help="latency or cost."),
    minimum_improvement: float = typer.Option(
        0, min=0, help="Required percentage gain in the selected objective."
    ),
):
    """Freeze task checks and original findings before editing the agent."""
    try:
        specification = improvement.start(
            tasks,
            baseline,
            out,
            focus=focus,
            objective=objective,
            minimum_improvement=minimum_improvement,
        )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(
        f"Investigation focus is {specification['focus'] or 'unavailable'}.", markup=False
    )
    console.print(
        f"Review {out / 'plan.md'}, apply your change, then capture the same tasks again.",
        markup=False,
    )
    console.print(f"Frozen session written to {out / 'session.json'}", markup=False)


@improve_app.command("finish")
def finish_command(
    session: Path = typer.Option(..., help="Frozen session.json."),
    candidate: Path = typer.Option(..., help="Candidate capture.json recorded after start."),
    change: str = typer.Option(..., help="Describe the change you applied."),
    out: Path = typer.Option(..., help="Fresh report directory."),
):
    """Compare every planned attempt and write JSON, Markdown and HTML results."""
    try:
        report = improvement.finish(session, candidate, out, change=change)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(
        f"{report['passed_attempts']} of {report['planned_attempts']} planned comparisons passed.",
        markup=False,
    )
    console.print(f"Read {out / 'report.html'}", markup=False)
    if not report["passed"]:
        raise typer.Exit(1)
