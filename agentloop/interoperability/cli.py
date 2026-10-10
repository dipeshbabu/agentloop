"""Offline batch telemetry import; no collectors, SDKs or providers are started."""

from __future__ import annotations

from pathlib import Path

import typer

from agentloop.interoperability.validation import ImportValidationError

telemetry_app = typer.Typer(help="Import bounded OTLP JSON/JSONL evidence offline.")


@telemetry_app.command("import-jsonl")
def import_jsonl_command(
    path: Path,
    out: Path = typer.Option(
        ..., help="Output directory for qualified traces, receipts and inventory."
    ),
    source: str = typer.Option("otel", help="Source system: otel, harbor or omnigent."),
    strict: bool = typer.Option(False, help="Fail on an independent malformed record."),
    single_json: bool = typer.Option(
        False, help="Read one bounded JSON document instead of JSONL."
    ),
    synthetic: bool = typer.Option(False, help="Explicitly mark owned synthetic fixtures."),
) -> None:
    """Stream a completed export and preserve every invalid/conflicting record."""
    from agentloop.interoperability.otlp_jsonl import OtlpOptions, import_otlp

    try:
        result = import_otlp(
            path,
            options=OtlpOptions(
                system=source, continue_on_error=not strict, synthetic_fixture=synthetic
            ),
            jsonl=not single_json,
        )
    except (ImportValidationError, OSError) as exc:
        message = (
            str(exc)
            if isinstance(exc, ImportValidationError)
            else "Telemetry artifacts could not be read"
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
        f"{stats['valid_records']} records imported; {stats['invalid_records']} invalid; {stats['native_traces']} trace projections; {stats['identical_duplicate_spans']} identical duplicate spans; {stats['conflicting_span_segments']} conflicting segments."
    )
    typer.echo(
        "Source spans/evaluations are externally reported. Exported telemetry alone establishes no task-correctness gate."
    )
    typer.echo(f"Inventory: {inventory}")
