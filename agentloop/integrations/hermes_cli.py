"""Package the Hermes observer for explicit installation by the user."""

from __future__ import annotations

from pathlib import Path

import typer

from agentloop.integrations.hermes import export_hermes_plugin

hermes_app = typer.Typer(help="Create a local, dependency-free Hermes observer plugin.")


@hermes_app.command("export-plugin")
def export_plugin(directory: Path) -> None:
    """Create a fresh directory plugin for Hermes 0.21.6."""
    try:
        path = export_hermes_plugin(directory)
    except (ValueError, OSError) as exc:
        raise typer.BadParameter(str(exc), param_hint="directory") from None
    typer.echo(f"Created AgentLoop Hermes plugin at {path}")
    typer.echo("Copy this directory into your active Hermes profile's plugins/ directory.")
    typer.echo("Then run: hermes plugins enable agentloop")
