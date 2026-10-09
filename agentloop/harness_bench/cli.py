"""Capability inspection commands; enumeration never invokes an adapter."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from agentloop.interoperability.validation import ImportValidationError

harness_app = typer.Typer(help="Inspect declared and behavior-verified harness capabilities.")


@harness_app.command("bench")
def bench_command(
    adapter: str = typer.Option("python-wrapped"),
    mode: str = typer.Option("offline"),
    out: Path = typer.Option(Path("runs/capability-bench")),
    json_out: Path | None = typer.Option(None),
    test_timestamp: str | None = typer.Option(None),
) -> None:
    """Run trusted offline probes; optional/live features remain explicitly skipped."""
    from agentloop.harness_bench.offline import run_offline_bench

    if mode != "offline":
        raise typer.BadParameter(
            "Only the approved offline probe mode is available", param_hint="--mode"
        )
    try:
        result = run_offline_bench(adapter, test_timestamp=test_timestamp)
        path = result.write(out)
        if json_out is not None:
            json_out.parent.mkdir(parents=True, exist_ok=True)
            json_out.write_text(json.dumps(result.report(), indent=2) + "\n", encoding="utf-8")
    except (ImportValidationError, OSError) as exc:
        message = (
            str(exc)
            if isinstance(exc, ImportValidationError)
            else "Bench output could not be written"
        )
        raise typer.BadParameter(message, param_hint="--adapter/--out") from None
    report = result.report()
    typer.echo(
        f"Offline probe report: {path}; {len(report['drift'])} declaration/behavior contradictions. Scope: explicitly wrapped calls only."
    )
    if report["drift"] or report["status"] == "failed":
        raise typer.Exit(1)


@harness_app.command("list-adapters")
def list_adapters_command() -> None:
    from agentloop.interoperability.registry import builtin_adapters

    typer.echo(json.dumps([descriptor.to_dict() for descriptor in builtin_adapters()], indent=2))


@harness_app.command("capabilities")
def capabilities_command(
    adapter: str = typer.Option(...), json_out: Path | None = typer.Option(None)
) -> None:
    from agentloop.interoperability.registry import capability_report, get_adapter

    try:
        result = capability_report(get_adapter(adapter))
    except ImportValidationError as exc:
        raise typer.BadParameter(str(exc), param_hint="--adapter") from None
    content = json.dumps(result, indent=2)
    if json_out is not None:
        try:
            json_out.parent.mkdir(parents=True, exist_ok=True)
            json_out.write_text(content + "\n", encoding="utf-8")
        except OSError:
            raise typer.BadParameter(
                "Capability output could not be written", param_hint="--json-out"
            ) from None
    typer.echo(content)
