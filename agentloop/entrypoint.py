from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from agentloop.cli import _build_quality_report_from_file, _load_trace, _write_json, app, console
from agentloop.events import AgentEvent
from agentloop.findings import build_diagnosis
from agentloop.html_report import analysis_to_html
from agentloop.optimizer import build_optimization_plan
from agentloop.replay import ReplayGates, build_replay_report
from agentloop.tracer import AgentTrace


def _quickstart_trace() -> AgentTrace:
    """Build a deterministic synthetic trace with several obvious bottlenecks."""
    run_id = "run_agentloop_quickstart"
    trace = AgentTrace(
        name="agentloop_quickstart",
        run_id=run_id,
        metadata={"synthetic": True, "source": "agentloop_quickstart"},
        started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:00:01+00:00",
        elapsed_ms=1000.0,
    )
    events = [
        ("tool_call", "retrieve", 0.0, 180.0, None, 0, 0),
        ("tool_call", "retrieve", 180.0, 360.0, None, 0, 0),
        ("tool_call", "retrieve", 360.0, 540.0, None, 0, 0),
        ("model_call", "summarize", 540.0, 650.0, "gpt-4.1", 700, 80),
        ("model_call", "summarize", 650.0, 760.0, "gpt-4.1", 700, 80),
        ("model_call", "summarize", 760.0, 870.0, "gpt-4.1", 700, 80),
        ("retry", "repair_json", 870.0, 930.0, None, 0, 0),
    ]
    for index, (
        event_type,
        name,
        start_ms,
        end_ms,
        model,
        input_tokens,
        output_tokens,
    ) in enumerate(events, start=1):
        start_seconds = start_ms / 1000
        end_seconds = end_ms / 1000
        trace.add_event(
            AgentEvent(
                event_id=f"evt_quickstart_{index:02d}",
                run_id=run_id,
                event_type=event_type,
                name=name,
                started_at=f"2026-01-01T00:00:{start_seconds:06.3f}+00:00",
                ended_at=f"2026-01-01T00:00:{end_seconds:06.3f}+00:00",
                duration_ms=end_ms - start_ms,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                input_text=("stable research instruction " * 30 if model else None),
            )
        )
    return trace


def _print_analysis(trace: AgentTrace, diagnosis: dict, plan: dict) -> None:
    report = trace.report()
    summary = diagnosis["summary"]
    table = Table(title=f"AgentLoop Analysis: {trace.name}")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("Runtime", f"{report['total_runtime_ms'] / 1000:.3f}s")
    table.add_row("Findings", str(summary["finding_count"]))
    table.add_row("High severity", str(summary["high_severity_count"]))
    table.add_row("Patchable", str(summary["patchable_count"]))
    table.add_row("Retries", str(report["retry_count"]))
    table.add_row("Input tokens", str(report["input_tokens"]))
    console.print(table)

    if report.get("rule_errors"):
        console.print(
            "Analysis incomplete; failed rules: "
            + ", ".join(error["rule_id"] for error in report["rule_errors"]),
            markup=False,
        )

    findings = diagnosis.get("findings", [])
    if not findings:
        console.print("No optimization findings detected.")
        return
    console.print("\nTop findings:")
    for finding in findings[:5]:
        console.print(
            f"- [{finding['severity']}] {finding['title']} ({finding['confidence']} confidence)"
        )
    aggregation = plan.get("savings_aggregation", {})
    if aggregation.get("selection_optimal") is False:
        console.print("Savings selection includes an approximate large-component fallback.")


def _analysis_payload(trace: AgentTrace) -> dict:
    report = trace.report()
    diagnosis = build_diagnosis(trace)
    plan = build_optimization_plan(trace, report=report)
    return {
        "trace": trace.to_dict(),
        "report": report,
        "diagnosis": diagnosis,
        "optimization": plan,
    }


@app.command("quickstart")
def quickstart_command(
    out: Path = typer.Option(
        Path("runs/agentloop_quickstart.json"),
        help="Where to write the deterministic synthetic trace.",
    ),
    json_out: Path | None = typer.Option(
        None, help="Optional path for the combined analysis JSON."
    ),
) -> None:
    """Generate one offline trace and show useful findings with no account or API key."""
    trace = _quickstart_trace()
    trace.export_json(out)
    payload = _analysis_payload(trace)
    _print_analysis(trace, payload["diagnosis"], payload["optimization"])
    console.print(f"\nSynthetic trace: {out}")
    console.print(f"Next: agentloop analyze {out}")
    if json_out is not None:
        _write_json(json_out, payload)
        console.print(f"Analysis JSON: {json_out}")


@app.command("analyze")
def analyze_command(
    path: Path,
    json_out: Path | None = typer.Option(
        None, help="Optional path for report, diagnosis, and optimization JSON."
    ),
    html_out: Path | None = typer.Option(
        None, "--html", help="Write a single offline HTML report."
    ),
    baseline: Path | None = typer.Option(
        None, help="Optional baseline trace; the positional trace is the candidate."
    ),
    quality_fixtures: Path | None = typer.Option(
        None, help="Quality fixtures for the optional baseline comparison."
    ),
    min_quality_score: float | None = typer.Option(None, min=0, max=1),
    include_content: bool = typer.Option(
        False, help="Include raw event text and full event metadata in HTML."
    ),
) -> None:
    """Analyze one existing AgentLoop trace in a single command."""
    trace = _load_trace(path, param_hint="path")
    if (quality_fixtures is not None or min_quality_score is not None) and baseline is None:
        raise typer.BadParameter("quality comparison options require --baseline")
    if include_content and html_out is None:
        raise typer.BadParameter("--include-content requires --html")
    payload = _analysis_payload(trace)
    if baseline is not None:
        baseline_trace = _load_trace(baseline, param_hint="--baseline")
        quality = (
            _build_quality_report_from_file(
                quality_fixtures,
                param_hint="--quality-fixtures",
                baseline_trace=baseline_trace,
                candidate_trace=trace,
                min_score=min_quality_score,
            )
            if quality_fixtures is not None
            else None
        )
        payload["replay"] = build_replay_report(
            baseline_trace,
            trace,
            gates=ReplayGates(min_quality_score=min_quality_score),
            quality_report=quality,
        )
    _print_analysis(trace, payload["diagnosis"], payload["optimization"])
    if json_out is not None:
        _write_json(json_out, payload)
        console.print(f"Wrote analysis JSON to {json_out}")
    if html_out is not None:
        html_out.parent.mkdir(parents=True, exist_ok=True)
        html_out.write_text(
            analysis_to_html(payload, include_content=include_content), encoding="utf-8"
        )
        console.print(f"Wrote HTML report to {html_out}")
