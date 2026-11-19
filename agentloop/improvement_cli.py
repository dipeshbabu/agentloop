"""Guided command-line entry points for user-owned agent workflows."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from agentloop import improvement
from agentloop.improvement_init import initialize, project_tasks
from agentloop.improvement_report import (
    _headline,
    comparison_label,
    decision_objective,
    runner_gate_summary,
    timing_summary_rows,
)
from agentloop.improvement_verifier import DEFAULT_TIMEOUT

improve_app = typer.Typer(
    help="Capture fixed tasks, review baseline findings, and verify an applied change."
)
console = Console()


def tasks_path(tasks: Path | None, project: Path) -> Path:
    return tasks if tasks is not None else project_tasks(project)


@improve_app.command("init")
def init_command(
    out: Path = typer.Option(Path("."), help="Existing project directory or a new directory."),
    agent: str | None = typer.Option(None, help="Existing Python package.module:callable."),
    name: str | None = typer.Option(None, help="Name shown in the trace and reports."),
):
    """Create task data, a Python adapter, and an executable offline example."""
    try:
        files = initialize(out, target=agent, name=name)
    except (OSError, ValueError, TypeError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"Created {len(files)} files in {out.resolve()}", markup=False)
    console.print(
        "Read AGENTLOOP.md, then run python agentloop_runner.py --check from that directory.",
        markup=False,
    )


@improve_app.command("capture")
def capture_command(
    tasks: Path | None = typer.Option(None, help="Fixed tasks, overriding the project config."),
    project: Path = typer.Option(
        Path("agentloop.toml"), help="Data-only project config used when --tasks is omitted."
    ),
    out: Path = typer.Option(..., help="Fresh output directory for this condition."),
    timeout: float = typer.Option(180, min=0.01, max=3600, help="Seconds per task attempt."),
    show_runner_output: bool = typer.Option(
        False, help="Stream the runner's stdout and stderr for diagnosis."
    ),
    control_arg: list[str] | None = typer.Option(
        None,
        "--control-arg",
        help="One argument of the unchanged control runner; repeat for each argument. "
        "Alternates control and candidate attempts so drift affects both.",
    ),
    command: list[str] = typer.Argument(
        ..., help="Explicit runner argv after --. Receives a JSON request on stdin."
    ),
):
    """Run every declared task and retain failed, missing and interrupted attempts."""
    try:
        manifest = improvement.capture(
            tasks_path(tasks, project),
            out,
            command,
            timeout=timeout,
            show_output=show_runner_output,
            control=control_arg or None,
        )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    attempts = [("", row) for row in manifest["runs"]]
    if control_arg:
        control = improvement.load_json(out.resolve() / improvement.CONTROL_CAPTURE)
        attempts += [("Control: ", row) for row in control["runs"]]
    complete = sum(row["status"] == "completed" for _, row in attempts)
    console.print(f"Captured {complete} of {len(attempts)} planned attempts.", markup=False)
    console.print(f"Capture manifest written to {out / 'capture.json'}", markup=False)
    for prefix, attempt in attempts:
        if attempt["status"] != "completed":
            console.print(
                f"{prefix}{attempt['task_id']} repetition {attempt['repetition']} — {attempt['status']}. {attempt.get('diagnostic') or 'Inspect the runner with --show-runner-output.'}",
                markup=False,
            )
    if complete != len(attempts):
        raise typer.Exit(1)


@improve_app.command("start")
def start_command(
    tasks: Path | None = typer.Option(None),
    project: Path = typer.Option(Path("agentloop.toml")),
    baseline: Path = typer.Option(..., help="Baseline capture.json."),
    out: Path = typer.Option(..., help="Fresh session directory."),
    focus: str | None = typer.Option(
        None, help="Finding type to investigate. Defaults to the first baseline finding."
    ),
    objective: str = typer.Option("latency", help="latency or cost."),
    minimum_improvement: float = typer.Option(
        0, min=0, help="Required percentage gain in the selected objective."
    ),
    max_runner_regression_ms: float | None = typer.Option(
        None, min=0, help="Optional maximum runner wall-time increase in ms for each pair."
    ),
    decision: str = typer.Option(
        "per-pair",
        help="per-pair requires every pair to improve; aggregate judges the median "
        "paired improvement for noisy agents. Correctness is checked per pair in both.",
    ),
    verifier: Path | None = typer.Option(None, help="Explicit trusted local Python verifier file."),
    verifier_timeout: float = typer.Option(
        DEFAULT_TIMEOUT,
        min=0.01,
        max=3600,
        help="Seconds per verifier execution, frozen in the session.",
    ),
):
    """Freeze task checks and original findings before editing the agent."""
    try:
        specification = improvement.start(
            tasks_path(tasks, project),
            baseline,
            out,
            focus=focus,
            objective=objective,
            minimum_improvement=minimum_improvement,
            max_runner_regression_ms=max_runner_regression_ms,
            decision=decision,
            verifier=verifier,
            verifier_timeout=verifier_timeout,
        )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(
        f"Investigation focus is {specification['focus'] or 'unavailable'}.", markup=False
    )
    _print_planning_notes(specification)
    console.print(
        f"Review {out / 'plan.md'}, apply your change, then capture the same tasks again.",
        markup=False,
    )
    console.print(f"Frozen session written to {out / 'session.json'}", markup=False)
    if verifier is not None:
        passed = sum(case["baseline_verifier"]["passed"] for case in specification["cases"])
        total = len(specification["cases"])
        console.print(
            f"Local verifier passed on {passed} of {total} baseline attempts.", markup=False
        )


@improve_app.command("finish")
def finish_command(
    session: Path = typer.Option(..., help="Frozen session.json."),
    candidate: Path = typer.Option(..., help="Candidate capture.json recorded after start."),
    change: str = typer.Option(..., help="Describe the change you applied."),
    out: Path = typer.Option(..., help="Fresh report directory."),
    verifier: Path | None = typer.Option(
        None, help="Explicit trusted Python verifier matching the frozen session."
    ),
):
    """Compare every planned attempt and write JSON, Markdown and HTML results."""
    try:
        report = improvement.finish(session, candidate, out, change=change, verifier=verifier)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    _print_report(report, out)


def _print_planning_notes(specification):
    for note in specification.get("planning_notes", []):
        console.print(f"Note: {note['message']}", markup=False)


def _print_report(report, out):
    """Print the verdict and timing summary; exit 1 when the change is rejected."""
    verdict = report["decision"]
    if verdict["mode"] == "aggregate":
        console.print(_headline(report), markup=False)
        median = verdict["median_improvement_pct"]
        console.print(
            f"Aggregate {decision_objective(verdict)} check "
            + ("passed" if verdict["passed"] else "failed")
            + f": median improvement {'unavailable' if median is None else f'{median:.2f}%'}, "
            f"{verdict['improved_pairs']} improved, {verdict['regressed_pairs']} regressed.",
            markup=False,
        )
        for reason in verdict["reasons"]:
            console.print(f"Aggregate check failed: {reason}", markup=False)
    else:
        console.print(
            f"{report['passed_attempts']} of {report['planned_attempts']} planned comparisons passed.",
            markup=False,
        )
    if summary := runner_gate_summary(report):
        console.print(summary, markup=False)
    timing = Table(
        "Timing",
        f"{comparison_label(report)} median",
        "Candidate median",
        "Measured pairs",
        caption="Completed, correct pairs. The latency objective uses trace time.",
    )
    for label, before, after, _, pairs in timing_summary_rows(report):
        timing.add_row(label, before, after, pairs)
    console.print(timing)
    console.print(f"Read {out / 'report.html'}", markup=False)
    for case in report["cases"]:
        if not case["passed"]:
            console.print(
                f"{case['task_id']} repetition {case['repetition']} rejected — "
                + " ".join(case["reasons"]),
                markup=False,
            )
    if not report["passed"]:
        raise typer.Exit(1)


@improve_app.command("compare")
def compare_command(
    tasks: Path | None = typer.Option(None, help="Fixed tasks, overriding the project config."),
    project: Path = typer.Option(
        Path("agentloop.toml"), help="Data-only project config used when --tasks is omitted."
    ),
    out: Path = typer.Option(..., help="Fresh directory for every stage of the comparison."),
    before_arg: list[str] = typer.Option(
        ...,
        "--before-arg",
        help="One argument of the unchanged before runner; repeat for each argument. "
        "It runs the baseline and then alternates with the after runner as a control.",
    ),
    change: str = typer.Option(..., help="Describe the change the after runner applies."),
    timeout: float = typer.Option(180, min=0.01, max=3600, help="Seconds per task attempt."),
    show_runner_output: bool = typer.Option(
        False, help="Stream the runners' stdout and stderr for diagnosis."
    ),
    focus: str | None = typer.Option(
        None, help="Finding type to investigate. Defaults to the first baseline finding."
    ),
    objective: str = typer.Option("latency", help="latency or cost."),
    minimum_improvement: float = typer.Option(
        0, min=0, help="Required percentage gain in the selected objective."
    ),
    max_runner_regression_ms: float | None = typer.Option(
        None, min=0, help="Optional maximum runner wall-time increase in ms for each pair."
    ),
    decision: str = typer.Option(
        "aggregate",
        help="aggregate (default here) judges the median paired improvement; per-pair "
        "requires every pair to improve. Correctness is checked per pair in both.",
    ),
    verifier: Path | None = typer.Option(None, help="Explicit trusted local Python verifier file."),
    verifier_timeout: float = typer.Option(
        DEFAULT_TIMEOUT, min=0.01, max=3600, help="Seconds per verifier execution."
    ),
    command: list[str] = typer.Argument(
        ..., help="Explicit after-runner argv after --. Receives a JSON request on stdin."
    ),
):
    """Capture, freeze, interleave and compare an unchanged and a changed runner."""
    try:
        report = improvement.compare(
            tasks_path(tasks, project),
            out,
            before_arg,
            command,
            change=change,
            timeout=timeout,
            show_output=show_runner_output,
            focus=focus,
            objective=objective,
            minimum_improvement=minimum_improvement,
            decision=decision,
            verifier=verifier,
            verifier_timeout=verifier_timeout,
            max_runner_regression_ms=max_runner_regression_ms,
            progress=lambda message: console.print(message, markup=False),
        )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    session = out / improvement.COMPARE_LAYOUT["session"]
    console.print(f"Baseline findings and plan: {session / 'plan.md'}", markup=False)
    _print_report(report, out / improvement.COMPARE_LAYOUT["results"])
