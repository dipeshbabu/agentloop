"""Read-only Omnigent file import and exported-session inspection commands."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from agentloop.interoperability.validation import ImportValidationError

omnigent_app = typer.Typer(help="Import Omnigent OTLP files and inspect session evidence offline.")


@omnigent_app.command("import-otel")
def import_otel_command(
    path: Path,
    out: Path = typer.Option(...),
    strict: bool = typer.Option(False),
    synthetic: bool = typer.Option(False),
) -> None:
    from agentloop.integrations.omnigent.telemetry import import_omnigent
    from agentloop.interoperability.otlp_jsonl import OtlpOptions

    try:
        result = import_omnigent(
            path,
            options=OtlpOptions(
                system="omnigent", continue_on_error=not strict, synthetic_fixture=synthetic
            ),
        )
    except (ImportValidationError, OSError) as exc:
        message = (
            str(exc)
            if isinstance(exc, ImportValidationError)
            else "Omnigent artifacts could not be read"
        )
        raise typer.BadParameter(message, param_hint="path") from None
    try:
        inventory = result.write(out)
    except (ImportValidationError, OSError) as exc:
        message = (
            str(exc) if isinstance(exc, ImportValidationError) else "Output could not be written"
        )
        raise typer.BadParameter(message, param_hint="--out") from None
    stats = result.inventory()
    typer.echo(
        f"{stats['native_traces']} trace projections; {stats['session_groups']} session groups; {stats['invalid_records']} invalid records."
    )
    typer.echo(
        "Policy decisions are observations; dispatch prevention and task correctness remain unverified."
    )
    typer.echo(f"Inventory: {inventory}")


@omnigent_app.command("inspect")
def inspect_command(path: Path, json_out: Path | None = typer.Option(None)) -> None:
    from agentloop.integrations.omnigent.telemetry import inspect_omnigent_bundle

    try:
        result = inspect_omnigent_bundle(path)
    except (ImportValidationError, OSError) as exc:
        message = (
            str(exc)
            if isinstance(exc, ImportValidationError)
            else "Import bundle could not be read"
        )
        raise typer.BadParameter(message, param_hint="path") from None
    content = json.dumps(result, indent=2)
    if json_out is not None:
        try:
            json_out.parent.mkdir(parents=True, exist_ok=True)
            json_out.write_text(content + "\n", encoding="utf-8")
        except OSError:
            raise typer.BadParameter(
                "Inspection output could not be written", param_hint="--json-out"
            ) from None
    typer.echo(content)
