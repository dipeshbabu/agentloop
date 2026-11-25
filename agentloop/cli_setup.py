"""Environment, production-readiness and integration checks.

Split out of ``agentloop.cli`` (issue #407). Commands register on
``agentloop.cli.app``; shared helpers are looked up through ``agentloop.cli`` at
call time, so tests and callers that patch them there keep working.
"""

from __future__ import annotations

from pathlib import Path

import typer

from agentloop import cli
from agentloop.autoinstrument import detect_integrations
from agentloop.doctor import run_doctor, run_production_check


@cli.app.command("doctor")
def doctor_command(
    check_api: bool = typer.Option(True, help="Also call the configured /health endpoint."),
    json_out: Path | None = typer.Option(
        None, help="Optional path for machine-readable doctor output."
    ),
) -> None:
    result = run_doctor(check_api=check_api)
    if json_out:
        cli._write_json(json_out, result)
        cli.console.print(f"Wrote doctor output to {json_out}")
    cli._print_doctor(result)


@cli.app.command("production-check")
def production_check_command(
    check_api: bool = typer.Option(True, help="Call the configured /health and /readyz endpoints."),
    check_store: bool = typer.Option(True, help="Initialize and query the configured store."),
    allow_http: bool = typer.Option(
        False, help="Allow non-HTTPS AGENTLOOP_API_URL for local staging."
    ),
    json_out: Path | None = typer.Option(
        None, help="Optional path for machine-readable check output."
    ),
) -> None:
    result = run_production_check(
        check_api=check_api, check_store=check_store, allow_http=allow_http
    )
    if json_out:
        cli._write_json(json_out, result)
        cli.console.print(f"Wrote production check output to {json_out}")
    cli._print_doctor(result)


@cli.app.command("detect-integrations")
def detect_integrations_command(json_out: Path | None = typer.Option(None)) -> None:
    """Report which framework integrations are available in this environment.

    Detection only: it checks whether each integration's SDK is importable and
    does not instrument anything. Apply agentloop.integrations helpers from your
    application startup to record traces.
    """
    result = detect_integrations().to_dict()
    if json_out:
        cli._write_json(json_out, result)
        cli.console.print(f"Wrote integration detection output to {json_out}")
    cli.console.print_json(data=result)


@cli.app.command("auto-instrument", hidden=True, deprecated=True)
def auto_instrument_command(json_out: Path | None = typer.Option(None)) -> None:
    """Deprecated alias for `detect-integrations`; it only detects, never instruments."""
    cli.console.print(
        "[yellow]`auto-instrument` is deprecated and never enabled instrumentation; "
        "use `detect-integrations`.[/yellow]"
    )
    detect_integrations_command(json_out=json_out)
