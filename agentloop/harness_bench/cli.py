"""Capability inspection commands; enumeration never invokes an adapter."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from agentloop.interoperability.validation import ImportValidationError

harness_app = typer.Typer(help="Inspect declared and behavior-verified harness capabilities.")


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
