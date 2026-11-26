"""Onboarding and offline study commands.

Split out of ``agentloop.cli`` (issue #407). Commands register on
``agentloop.cli.app``; shared helpers are looked up through ``agentloop.cli`` at
call time, so tests and callers that patch them there keep working.
"""

from __future__ import annotations

from pathlib import Path

import typer

from agentloop import cli
from agentloop.ablation_protocol import AblationValidationError
from agentloop.ablations import ablation_to_markdown, summarize_ablation
from agentloop.calibration import (
    CalibrationValidationError,
    calibration_to_markdown,
    summarize_calibration,
)
from agentloop.cli_paths import validate_output_paths
from agentloop.studies import StudyValidationError, study_to_markdown, summarize_study


@cli.app.command("onboard")
def onboard_command(
    path: Path,
    format: str = typer.Option(
        "native", help="native or otlp; OTLP includes supported OpenInference attributes."
    ),
    out: Path = typer.Option(
        Path("runs/onboarding.json"), help="Validation and payload-free analysis summary."
    ),
    expected_operation: list[str] | None = typer.Option(
        None, help="Repeat for expected model/tool/classifier/etc. kinds."
    ),
    analyze: bool = typer.Option(
        True, help="Analyze after validation; limited to 2000 spans per trace."
    ),
) -> None:
    """Validate captured evidence and analyze existing telemetry without application changes."""
    from agentloop.aggregates import read_json
    from agentloop.onboarding import onboard

    validate_output_paths(sources=[path], outputs={"--out": out})
    try:
        result = onboard(
            read_json(path),
            format=format,
            expected_operations=tuple(expected_operation or ()),
            analyze=analyze,
        )
    except (OSError, ValueError, TypeError, OverflowError) as exc:
        raise typer.BadParameter(str(exc), param_hint="path/format") from None
    cli._write_json(out, result)
    cli.console.print(
        f"Capture status: {result['status']}; traces: {result['trace_count']}", markup=False
    )
    for item in result["traces"]:
        cli.console.print(
            f"Trace {item['run_id']}: {item['validation']['status']}; analysis {item['analysis_status']}",
            markup=False,
        )
        for check in item["validation"]["checks"]:
            cli.console.print(
                f"- {check['code']} ({check['count']}): {check['detail']}", markup=False
            )
        if item["analysis"] is not None:
            report = item["analysis"]
            cli.console.print(
                f"  Runtime {report['total_runtime_ms']} ms; model calls {report['model_call_count']}; token basis {report['token_status']}; findings {len(item['findings'])}",
                markup=False,
            )
    cli.console.print(f"Wrote onboarding evidence to {out}")
    if result["status"] in {"invalid", "no_execution_data"}:
        raise typer.Exit(code=1)


@cli.study_app.command("ablation")
def study_ablation_command(
    bundle: Path,
    out: Path = typer.Option(Path("runs/ablation.md"), help="Readable ablation report."),
    json_out: Path = typer.Option(
        Path("runs/ablation.json"), help="Full observations and denominators."
    ),
) -> None:
    try:
        report = summarize_ablation(bundle)
    except AblationValidationError as exc:
        raise typer.BadParameter(str(exc), param_hint="bundle") from None
    validate_output_paths(
        sources=[bundle, *(Path(source) for source in report["source_paths"])],
        outputs={"--out": out, "--json-out": json_out},
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(ablation_to_markdown(report), encoding="utf-8")
    cli._write_json(json_out, report)
    cli.console.print(f"Wrote ablation reports to {out} and {json_out}")


@cli.study_app.command("calibrate")
def study_calibrate_command(
    manifest: Path,
    out: Path = typer.Option(Path("runs/calibration.md"), help="Readable historical calibration."),
    json_out: Path = typer.Option(
        Path("runs/calibration.json"), help="Versioned calibration artifact."
    ),
) -> None:
    try:
        report = summarize_calibration(manifest)
    except CalibrationValidationError as exc:
        raise typer.BadParameter(str(exc), param_hint="manifest") from None
    validate_output_paths(
        sources=[manifest, *(Path(source) for source in report["source_paths"])],
        outputs={"--out": out, "--json-out": json_out},
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(calibration_to_markdown(report), encoding="utf-8")
    cli._write_json(json_out, report)
    cli.console.print(f"Wrote calibration reports to {out} and {json_out}")


@cli.study_app.command("summarize")
def study_summarize_command(
    manifest: Path,
    out: Path = typer.Option(Path("runs/study.md"), help="Human-readable study report."),
    json_out: Path = typer.Option(
        Path("runs/study.json"), help="Full study results and unmatched runs."
    ),
) -> None:
    try:
        report = summarize_study(manifest)
    except StudyValidationError as exc:
        raise typer.BadParameter(str(exc), param_hint="manifest") from None
    validate_output_paths(
        sources=[
            manifest,
            *(
                Path(run["path"])
                for condition in report["conditions"].values()
                for run in condition["runs"]
            ),
        ],
        outputs={"--out": out, "--json-out": json_out},
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(study_to_markdown(report), encoding="utf-8")
    cli._write_json(json_out, report)
    cli.console.print(f"Wrote study reports to {out} and {json_out}")
    for name, comparison in report["comparisons"].items():
        cli.console.print(
            f"{name}: {comparison['pair_count']} paired runs, {len(comparison['unmatched'])} unmatched runs"
        )
