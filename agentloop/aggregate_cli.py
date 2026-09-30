"""File-stream entry points; importing this module does not register global apps."""

from __future__ import annotations

from pathlib import Path

import typer

from agentloop.aggregates import MAX_ARTIFACT_BYTES, TraceAggregate, aggregate_files, read_json

aggregate_app = typer.Typer(
    help="Bounded-memory summaries of native traces and aggregate partitions."
)


def _paths(inputs, manifest, out):
    if not inputs and manifest is None:
        raise ValueError("supply trace paths or --manifest")
    destination = out.resolve()
    if manifest is not None and manifest.resolve() == destination:
        raise ValueError("output must differ from manifest")
    for path in inputs or []:
        if path.resolve() == destination:
            raise ValueError("output must differ from every input")
        yield path
    if manifest is not None:
        # JSONL strings, relative to the manifest. No unbounded list of filenames.
        import json

        with manifest.open("rb") as handle:
            while line := handle.readline(16_385):
                if len(line) > 16_384:
                    raise ValueError("manifest line exceeds 16384 bytes")
                value = json.loads(line)
                if not isinstance(value, str) or not value:
                    raise ValueError("manifest lines must be JSON path strings")
                path = manifest.parent / value
                if path.resolve() == destination:
                    raise ValueError("output must differ from every input")
                yield path


@aggregate_app.command("traces")
def aggregate_traces_command(
    inputs: list[Path] | None = typer.Argument(None),
    out: Path = typer.Option(Path("runs/aggregate.json")),
    manifest: Path | None = typer.Option(None, help="JSONL paths, relative to this manifest."),
    skip_invalid: bool = typer.Option(
        False, help="Count invalid inputs; do not retain error payloads."
    ),
    max_bytes: int = typer.Option(MAX_ARTIFACT_BYTES, min=1, max=1073741824),
):
    """Summarize one native trace file at a time, without graph/finding analysis."""
    from agentloop.cli import _write_json, console

    try:
        result = aggregate_files(
            _paths(inputs, manifest, out),
            on_error="skip" if skip_invalid else "raise",
            max_bytes=max_bytes,
        )
    except (OSError, ValueError, TypeError, OverflowError) as exc:
        raise typer.BadParameter(str(exc), param_hint="inputs") from None
    _write_json(out, result.to_dict())
    console.print(f"Wrote aggregate to {out}")


@aggregate_app.command("merge")
def merge_aggregates_command(
    inputs: list[Path] | None = typer.Argument(None),
    out: Path = typer.Option(Path("runs/merged-aggregate.json")),
    manifest: Path | None = typer.Option(None, help="JSONL partition paths."),
):
    """Merge disjoint partitions with matching schemas, configurations and policies."""
    from agentloop.cli import _write_json, console

    result = None
    try:
        for path in _paths(inputs, manifest, out):
            partition = TraceAggregate.from_dict(read_json(path))
            if result is None:
                result = partition
            else:
                result.merge(partition)
    except (OSError, ValueError, TypeError, OverflowError) as exc:
        raise typer.BadParameter(str(exc), param_hint="inputs") from None
    _write_json(out, (result or TraceAggregate()).to_dict())
    console.print(f"Wrote merged aggregate to {out}")
