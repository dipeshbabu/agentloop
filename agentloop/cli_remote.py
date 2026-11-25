"""Commands that talk to a remote AgentLoop server.

Split out of ``agentloop.cli`` (issue #407). Commands register on
``agentloop.cli.app``; shared helpers are looked up through ``agentloop.cli`` at
call time, so tests and callers that patch them there keep working.
"""

from __future__ import annotations

from pathlib import Path

import typer

from agentloop import cli
from agentloop.plan_export import export_optimization_json


@cli.app.command()
def upload(
    path: list[Path] = typer.Option(
        [Path("runs/research_agent_baseline.json")],
        "--path",
        help="Trace JSON file, or a directory of them; repeat for several.",
    ),
    label: list[str] | None = typer.Option(
        None,
        "--label",
        help="KEY=VALUE stored under metadata.labels of each uploaded trace, "
        "such as commit=abc123; repeat for several.",
    ),
    api_url: str = typer.Option("http://127.0.0.1:8000", help="AgentLoop API base URL."),
    api_key: str | None = typer.Option(
        None, help="Optional API key. Defaults to AGENTLOOP_API_KEY."
    ),
) -> None:
    """Upload traces to an AgentLoop API, optionally labelled with CI context."""
    labels = cli._parse_labels(label)
    files = cli._upload_files(path)
    traces = [cli._load_trace(file, param_hint="--path") for file in files]
    client = cli._remote_client(api_url, api_key)
    project = None
    for trace in traces:
        if labels:
            existing = trace.metadata.get("labels")
            trace.metadata["labels"] = {
                **(existing if isinstance(existing, dict) else {}),
                **labels,
            }
        response = client.upload_trace(trace)
        project = response.get("project_id", "default")
        cli.console.print(f"Uploaded trace {response['run_id']} to {api_url}", markup=False)
    if len(traces) > 1:
        cli.console.print(f"Uploaded {len(traces)} traces.", markup=False)
    cli.console.print(f"Project: {project}", markup=False)


@cli.app.command("remote-delete-trace")
def remote_delete_trace(
    run_id: str,
    api_url: str = typer.Option("http://127.0.0.1:8000", help="AgentLoop API base URL."),
    api_key: str | None = typer.Option(
        None, help="Project API key. Defaults to AGENTLOOP_API_KEY."
    ),
) -> None:
    """Delete a trace of the key's project on a remote server."""
    client = cli._remote_client(api_url, api_key)
    cli.console.print_json(data=client.delete_trace(run_id))


@cli.app.command("remote-optimize")
def remote_optimize(
    run_id: str,
    api_url: str = typer.Option("http://127.0.0.1:8000", help="AgentLoop API base URL."),
    api_key: str | None = typer.Option(
        None, help="Optional API key. Defaults to AGENTLOOP_API_KEY."
    ),
    out: Path = Path("runs/remote_optimization_plan.json"),
) -> None:
    client = cli._remote_client(api_url, api_key)
    plan = client.get_optimization_plan(run_id)
    export_optimization_json(plan, out)
    cli.console.print(f"Wrote remote optimization plan to {out}")


@cli.app.command(
    "remote-diagnose", help="Recompute and persist findings for a stored remote trace."
)
def remote_diagnose(
    run_id: str,
    api_url: str = typer.Option("http://127.0.0.1:8000", help="AgentLoop API base URL."),
    api_key: str | None = typer.Option(
        None, help="Optional API key. Defaults to AGENTLOOP_API_KEY."
    ),
    out: Path = Path("runs/remote_diagnosis.json"),
) -> None:
    client = cli._remote_client(api_url, api_key)
    diagnosis = client.save_diagnosis(run_id)
    cli._write_json(out, diagnosis)
    cli.console.print(f"Wrote remote diagnosis to {out}")


@cli.app.command("remote-findings")
def remote_findings(
    api_url: str = typer.Option("http://127.0.0.1:8000", help="AgentLoop API base URL."),
    api_key: str | None = typer.Option(
        None, help="Optional API key. Defaults to AGENTLOOP_API_KEY."
    ),
    project_id: str | None = typer.Option(None),
    status: str | None = typer.Option(None),
    out: Path = Path("runs/remote_findings.json"),
) -> None:
    client = cli._remote_client(api_url, api_key)
    findings = client.list_findings(project_id=project_id, status=status)
    cli._write_json(out, findings)
    cli.console.print(f"Wrote remote findings to {out}")


@cli.app.command("remote-optimization-queue")
def remote_optimization_queue(
    api_url: str = typer.Option("http://127.0.0.1:8000", help="AgentLoop API base URL."),
    api_key: str | None = typer.Option(
        None, help="Optional API key. Defaults to AGENTLOOP_API_KEY."
    ),
    project_id: str | None = typer.Option(None),
    out: Path = Path("runs/remote_optimization_queue.json"),
) -> None:
    client = cli._remote_client(api_url, api_key)
    queue = client.optimization_queue(project_id=project_id)
    cli._write_json(out, queue)
    cli.console.print(f"Wrote remote optimization queue to {out}")


@cli.app.command("remote-github-issue-drafts")
def remote_github_issue_drafts(
    api_url: str = typer.Option("http://127.0.0.1:8000", help="AgentLoop API base URL."),
    api_key: str | None = typer.Option(
        None, help="Optional API key. Defaults to AGENTLOOP_API_KEY."
    ),
    project_id: str | None = typer.Option(None),
    limit: int = typer.Option(5, min=1, max=20),
    out: Path = Path("runs/remote_agentloop_issue_drafts.json"),
) -> None:
    client = cli._remote_client(api_url, api_key)
    drafts = client.github_issue_drafts(project_id=project_id, limit=limit)
    cli._write_json(out, drafts)
    cli.console.print(f"Wrote remote GitHub issue drafts to {out}")


@cli.app.command("remote-value-report")
def remote_value_report(
    run_id: str,
    api_url: str = typer.Option("http://127.0.0.1:8000", help="AgentLoop API base URL."),
    api_key: str | None = typer.Option(
        None, help="Optional API key. Defaults to AGENTLOOP_API_KEY."
    ),
    out: Path = Path("runs/remote_value_report.json"),
    runs_per_month: int = typer.Option(1000, min=0),
    engineer_hourly_rate_usd: float = typer.Option(150.0, min=0),
    incident_cost_usd: float = typer.Option(500.0, min=0),
) -> None:
    client = cli._remote_client(api_url, api_key)
    value = client.get_value_report(
        run_id,
        runs_per_month=runs_per_month,
        engineer_hourly_rate_usd=engineer_hourly_rate_usd,
        incident_cost_usd=incident_cost_usd,
    )
    cli._write_json(out, value)
    cli._print_value_summary(value)
    cli.console.print(f"Wrote remote value report to {out}")


@cli.app.command("remote-usage")
def remote_usage(
    api_url: str = typer.Option("http://127.0.0.1:8000", help="AgentLoop API base URL."),
    api_key: str | None = typer.Option(
        None, help="Optional API key. Defaults to AGENTLOOP_API_KEY."
    ),
) -> None:
    client = cli._remote_client(api_url, api_key)
    cli.console.print_json(data=client.usage_summary())


@cli.app.command("remote-create-api-key")
def remote_create_api_key(
    project_id: str = "default",
    name: str = "default",
    api_url: str = typer.Option("http://127.0.0.1:8000", help="AgentLoop API base URL."),
    admin_api_key: str | None = typer.Option(
        None, help="Admin API key. Defaults to AGENTLOOP_ADMIN_API_KEY."
    ),
) -> None:
    client = cli._remote_admin_client(api_url, admin_api_key)
    key = client.create_api_key(project_id=project_id, name=name)
    cli.console.print("Created remote API key. Save it now; it will not be shown again.")
    cli.console.print(key["api_key"])


@cli.app.command("remote-list-api-keys")
def remote_list_api_keys(
    project_id: str = "default",
    api_url: str = typer.Option("http://127.0.0.1:8000", help="AgentLoop API base URL."),
    admin_api_key: str | None = typer.Option(
        None, help="Admin API key. Defaults to AGENTLOOP_ADMIN_API_KEY."
    ),
) -> None:
    """List a project's API key prefixes on a remote server."""
    client = cli._remote_admin_client(api_url, admin_api_key)
    cli.console.print_json(data=client.list_api_keys(project_id))


@cli.app.command("remote-audit-log")
def remote_audit_log(
    project_id: str | None = typer.Option(None, help="Only this project's events."),
    limit: int = typer.Option(50, min=1, max=1000, help="Newest events to show."),
    api_url: str = typer.Option("http://127.0.0.1:8000", help="AgentLoop API base URL."),
    admin_api_key: str | None = typer.Option(
        None, help="Admin API key. Defaults to AGENTLOOP_ADMIN_API_KEY."
    ),
) -> None:
    """Show a remote server's audit log (admin key required)."""
    client = cli._remote_admin_client(api_url, admin_api_key)
    cli.console.print_json(data=client.list_audit_events(project_id=project_id, limit=limit))


@cli.app.command("remote-revoke-api-key")
def remote_revoke_api_key(
    prefix: str = typer.Option(..., help="First 10 characters of the key, as listed."),
    project_id: str = "default",
    api_url: str = typer.Option("http://127.0.0.1:8000", help="AgentLoop API base URL."),
    admin_api_key: str | None = typer.Option(
        None, help="Admin API key. Defaults to AGENTLOOP_ADMIN_API_KEY."
    ),
) -> None:
    """Revoke a project's API key on a remote server."""
    client = cli._remote_admin_client(api_url, admin_api_key)
    result = client.revoke_api_key(project_id, prefix)
    cli.console.print(
        f"Revoked {result['revoked']} API key(s) with prefix {prefix} in project {project_id}.",
        markup=False,
    )
