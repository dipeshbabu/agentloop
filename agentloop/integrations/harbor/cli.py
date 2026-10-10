"""Offline Harbor commands; importing artifacts never runs a Harbor job."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from agentloop.interoperability.validation import ImportValidationError

harbor_app = typer.Typer(help="Import completed Harbor artifacts offline.")


def _job(
    path: Path,
    *,
    scoring: Path | None,
    condition: str | None,
    protocol_id: str | None,
    context: Path | None,
    synthetic: bool,
):
    from agentloop.integrations.harbor.trials import HarborOptions, import_harbor
    from agentloop.integrations.harbor.verifier import ScoringContract
    from agentloop.interoperability.validation import load_json_artifact

    try:
        criteria = (
            ScoringContract.from_dict(load_json_artifact(scoring.parent, scoring.name).payload)
            if scoring
            else None
        )
        declared = load_json_artifact(context.parent, context.name).payload if context else {}
        return import_harbor(
            path,
            options=HarborOptions(
                scoring=criteria,
                condition=condition,
                protocol_id=protocol_id,
                trial_context=declared,
                synthetic_fixture=synthetic,
            ),
        )
    except (ImportValidationError, OSError) as exc:
        message = (
            str(exc)
            if isinstance(exc, ImportValidationError)
            else "Job artifacts could not be read"
        )
        raise typer.BadParameter(message, param_hint="path") from None


@harbor_app.command("import")
def import_job_command(
    path: Path,
    out: Path = typer.Option(...),
    scoring: Path | None = typer.Option(
        None, help="Versioned explicit reward thresholds; no implicit positive-reward rule."
    ),
    condition: str | None = typer.Option(None),
    protocol_id: str | None = typer.Option(None),
    context: Path | None = typer.Option(
        None, help="Explicit trial-directory condition/protocol/repetition mapping."
    ),
    synthetic: bool = typer.Option(False),
) -> None:
    result = _job(
        path,
        scoring=scoring,
        condition=condition,
        protocol_id=protocol_id,
        context=context,
        synthetic=synthetic,
    )
    try:
        inventory = result.write(out)
    except (ImportValidationError, OSError) as exc:
        message = (
            str(exc) if isinstance(exc, ImportValidationError) else "Output could not be written"
        )
        raise typer.BadParameter(message, param_hint="--out") from None
    stats = result.inventory()
    typer.echo(
        f"{stats['trials_discovered']} trials retained; {stats['trials_imported']} imported; {stats['missing_trajectories']} missing trajectories; {stats['invalid_records']} invalid."
    )
    typer.echo(
        f"External criterion outcomes: {stats['quality_pass']} pass, {stats['quality_fail']} fail, {stats['quality_indeterminate']} indeterminate. Source hashes are reproducibility evidence, not authenticity."
    )
    typer.echo(f"Inventory: {inventory}")


@harbor_app.command("inspect")
def inspect_job_command(
    path: Path,
    json_out: Path | None = typer.Option(None),
    scoring: Path | None = typer.Option(None),
) -> None:
    result = _job(
        path, scoring=scoring, condition=None, protocol_id=None, context=None, synthetic=False
    )
    content = json.dumps(result.inventory(), indent=2)
    if json_out is not None:
        try:
            json_out.parent.mkdir(parents=True, exist_ok=True)
            json_out.write_text(content + "\n", encoding="utf-8")
        except OSError:
            raise typer.BadParameter(
                "Inspection output could not be written", param_hint="--json-out"
            ) from None
    typer.echo(content)


def _import(path: Path, *, strict: bool, root: Path | None, synthetic: bool = False):
    from agentloop.integrations.harbor.atif import AtifOptions, import_atif

    try:
        return import_atif(
            path, root=root, options=AtifOptions(strict=strict, synthetic_fixture=synthetic)
        )
    except (ImportValidationError, OSError) as exc:
        message = (
            str(exc) if isinstance(exc, ImportValidationError) else "Artifact could not be read"
        )
        raise typer.BadParameter(message, param_hint="path") from None


@harbor_app.command("import-atif")
def import_atif_command(
    path: Path,
    out: Path = typer.Option(
        ..., help="Output directory for native traces, receipts and inventory."
    ),
    root: Path | None = typer.Option(
        None, help="Allowed local reference root; defaults to the input directory."
    ),
    continue_on_error: bool = typer.Option(
        False, help="Retain semantically invalid documents as error receipts."
    ),
    synthetic: bool = typer.Option(False, help="Explicitly tag owned synthetic fixtures."),
) -> None:
    """Import ATIF v1.7/v1.8 and the documented v1.6 compatibility subset."""
    result = _import(path, strict=not continue_on_error, root=root, synthetic=synthetic)
    try:
        inventory = result.write(out)
    except (ImportValidationError, OSError) as exc:
        message = (
            str(exc) if isinstance(exc, ImportValidationError) else "Output could not be written"
        )
        raise typer.BadParameter(message, param_hint="--out") from None
    report = result.inventory()
    typer.echo(
        f"Imported {report['native_traces']} trajectories; {report['invalid_documents']} invalid; {report['timing_unavailable']} with unavailable timing."
    )
    typer.echo(
        "Source usage/status are externally reported; ATIF alone supplies no independent task-quality gate."
    )
    typer.echo(f"Inventory: {inventory}")


@harbor_app.command("inspect-atif")
def inspect_atif_command(
    path: Path,
    json_out: Path | None = typer.Option(None),
    root: Path | None = typer.Option(None),
) -> None:
    """Inspect a trajectory graph without creating native output artifacts."""
    result = _import(path, strict=False, root=root)
    payload = result.inventory()
    if json_out is not None:
        try:
            json_out.parent.mkdir(parents=True, exist_ok=True)
            json_out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        except OSError:
            raise typer.BadParameter(
                "Inspection output could not be written", param_hint="--json-out"
            ) from None
    typer.echo(json.dumps(payload, indent=2))
