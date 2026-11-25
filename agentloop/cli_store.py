"""Commands for the local trace store: keys, traces, findings and usage.

Split out of ``agentloop.cli`` (issue #407). Commands register on
``agentloop.cli.app``; shared helpers are looked up through ``agentloop.cli`` at
call time, so tests and callers that patch them there keep working.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.table import Table

from agentloop import cli
from agentloop.costs import format_cost_usd
from agentloop.issues import build_issue_drafts, issue_drafts_to_markdown
from agentloop.ranking import RankingSort, sort_ranked
from agentloop.store import (
    DEFAULT_PAGE_SIZE,
    FindingNotFoundError,
    FindingTransitionError,
    get_store,
    parse_label_filter,
)
from agentloop.timing import format_duration_ms


@cli.app.command("init-store")
def init_store() -> None:
    db = get_store()
    db.init()
    cli.console.print("Initialized AgentLoop store")


@cli.app.command("create-api-key")
def create_api_key(project_id: str = "default", name: str = "default") -> None:
    db = get_store()
    key = db.create_api_key(project_id=project_id, name=name, actor="cli")
    cli.console.print("Created API key. Save it now; it will not be shown again.")
    cli.console.print(key["api_key"])


@cli.app.command("list-api-keys")
def list_api_keys(project_id: str = "default") -> None:
    """List a project's API key prefixes and last use; key values are never shown."""
    cli.console.print_json(
        data={"project_id": project_id, "api_keys": get_store().list_api_keys(project_id)}
    )


@cli.app.command("revoke-api-key")
def revoke_api_key(
    prefix: str = typer.Option(..., help="First 10 characters of the key, as listed."),
    project_id: str = "default",
) -> None:
    """Revoke a project's API key; it stops working immediately."""
    try:
        revoked = get_store().revoke_api_key(project_id, prefix, actor="cli")
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if not revoked:
        cli.console.print(f"No API key with prefix {prefix} in project {project_id}.", markup=False)
        raise typer.Exit(1)
    cli.console.print(
        f"Revoked {revoked} API key(s) with prefix {prefix} in project {project_id}.", markup=False
    )


@cli.app.command("delete-trace")
def delete_trace(
    run_id: str,
    project_id: str = "default",
) -> None:
    """Delete a stored trace with its findings, usage and interventions."""
    deleted = get_store().delete_trace(run_id, project_id, actor="cli")
    if deleted is None:
        cli.console.print(f"No trace {run_id} in project {project_id}.", markup=False)
        raise typer.Exit(1)
    cli.console.print_json(data={"run_id": run_id, "project_id": project_id, "deleted": deleted})


@cli.app.command("purge-traces")
def purge_traces(
    older_than_days: float | None = typer.Option(
        None,
        min=0.0001,
        help="Delete traces stored more than this many days ago. "
        "Defaults to AGENTLOOP_RETENTION_DAYS.",
    ),
    project_id: str | None = typer.Option(None, help="Only purge this project."),
) -> None:
    """Apply a retention period: delete traces stored longer ago than allowed."""
    actor = "cli" if older_than_days is not None else "retention:AGENTLOOP_RETENTION_DAYS"
    if older_than_days is None:
        configured = os.getenv("AGENTLOOP_RETENTION_DAYS", "").strip()
        if not configured:
            cli.console.print(
                "Retention is not configured; set --older-than-days or AGENTLOOP_RETENTION_DAYS.",
                markup=False,
            )
            return
        try:
            older_than_days = float(configured)
        except ValueError:
            raise typer.BadParameter(
                "AGENTLOOP_RETENTION_DAYS must be a number of days",
                param_hint="AGENTLOOP_RETENTION_DAYS",
            ) from None
    try:
        deleted = get_store().purge_traces(older_than_days, project_id=project_id, actor=actor)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    scope = f"project {project_id}" if project_id else "all projects"
    cli.console.print(
        f"Deleted {deleted['traces']} trace(s) older than {older_than_days:g} days in {scope} "
        f"({deleted['findings']} findings, {deleted['usage_events']} usage events, "
        f"{deleted['interventions']} interventions).",
        markup=False,
        soft_wrap=True,
    )


@cli.app.command("audit-log")
def audit_log(
    project_id: str | None = typer.Option(None, help="Only this project's events."),
    limit: int = typer.Option(50, min=1, max=1000, help="Newest events to show."),
) -> None:
    """Show recent key, deletion and retention events from the store's audit log."""
    cli.console.print_json(
        data={
            "project_id": project_id,
            "events": get_store().list_audit_events(project_id, limit=limit),
        }
    )


@cli.app.command("store-trace")
def store_trace(
    path: Path = Path("runs/research_agent_baseline.json"),
    project_id: str = "default",
) -> None:
    trace = cli._load_trace(path, param_hint="--path")
    db = get_store()
    db.save_trace(trace, project_id=project_id)
    cli.console.print(f"Stored trace {trace.run_id} under project {project_id}")


@cli.app.command("list-stored-traces")
def list_stored_traces(
    project_id: str | None = None,
    limit: int | None = typer.Option(None, help="Page size; omit to list every stored trace."),
    cursor: str | None = typer.Option(
        None, help="Continuation cursor from a previous --limit call's printed next-cursor."
    ),
    label: str | None = typer.Option(
        None, help="Only traces with this KEY=VALUE label, such as commit=abc123."
    ),
) -> None:
    db = get_store()
    try:
        label_filter = parse_label_filter(label) if label is not None else None
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--label") from exc
    if limit is None and cursor is None and label_filter is None:
        traces = db.list_traces(project_id=project_id)
        next_cursor = None
    else:
        page = db.list_traces_page(
            project_id=project_id,
            limit=limit or DEFAULT_PAGE_SIZE,
            cursor=cursor,
            label=label_filter,
        )
        traces = page["items"]
        next_cursor = page["next_cursor"]
    table = Table(title="Stored AgentLoop Traces")
    table.add_column("Project")
    table.add_column("Run ID")
    table.add_column("Name")
    table.add_column("Runtime ms")
    table.add_column("Cost")
    show_labels = any(item.get("labels") for item in traces)
    if show_labels:
        table.add_column("Labels")
    for item in traces:
        row = [
            str(item.get("project_id", "")),
            str(item.get("run_id", "")),
            str(item.get("name", "")),
            "unavailable"
            if item.get("total_runtime_ms") is None
            else f"{float(item.get('total_runtime_ms', 0)):.2f}",
            format_cost_usd(item.get("estimated_cost_usd"), item.get("cost_status", "complete")),
        ]
        if show_labels:
            labels = item.get("labels") or {}
            row.append(", ".join(f"{key}={value}" for key, value in sorted(labels.items())))
        table.add_row(*row, end_section=False)
    cli.console.print(table)
    if next_cursor:
        cli.console.print(f"More results available. Next cursor: {next_cursor}")


@cli.app.command("list-findings")
def list_findings(
    project_id: str | None = None,
    status: str | None = None,
    limit: int | None = typer.Option(None, help="Page size; omit to list every matching finding."),
    cursor: str | None = typer.Option(
        None, help="Continuation cursor from a previous --limit call's printed next-cursor."
    ),
) -> None:
    db = get_store()
    if limit is None and cursor is None:
        findings = db.list_findings(project_id=project_id, status=status)
        next_cursor = None
    else:
        page = db.list_findings_page(
            project_id=project_id, status=status, limit=limit or DEFAULT_PAGE_SIZE, cursor=cursor
        )
        findings = page["items"]
        next_cursor = page["next_cursor"]
    table = Table(title="AgentLoop Findings")
    table.add_column("Severity")
    table.add_column("Type")
    table.add_column("Title")
    table.add_column("Run")
    table.add_column("Status")
    table.add_column("Patchable")
    table.add_column("Savings")
    for finding in findings:
        table.add_row(
            str(finding["severity"]),
            str(finding["type"]),
            str(finding["title"]),
            str(finding["run_id"]),
            str(finding["status"]),
            "yes" if finding["patchable"] else "no",
            f"{format_duration_ms(finding['estimated_latency_savings_ms'])} / "
            f"{format_cost_usd(finding.get('estimated_cost_savings_usd'))}",
        )
    cli.console.print(table)
    if next_cursor:
        cli.console.print(f"More results available. Next cursor: {next_cursor}")


@cli.app.command("update-finding-status")
def update_finding_status_command(
    run_id: str,
    finding_id: str,
    status: str,
    project_id: str = "default",
) -> None:
    """Transition a stored finding's lifecycle status (detected/accepted/resolved/dismissed)."""
    db = get_store()
    try:
        updated = db.update_finding_status(project_id, run_id, finding_id, status)
    except FindingNotFoundError as exc:
        raise typer.BadParameter(str(exc), param_hint="finding_id") from exc
    except (FindingTransitionError, ValueError) as exc:
        raise typer.BadParameter(str(exc), param_hint="status") from exc
    cli.console.print(f"Finding {finding_id} is now [bold]{updated['status']}[/bold]")


@cli.app.command("remote-update-finding-status")
def remote_update_finding_status(
    run_id: str,
    finding_id: str,
    status: str,
    api_url: str = typer.Option("http://127.0.0.1:8000", help="AgentLoop API base URL."),
    api_key: str | None = typer.Option(
        None, help="Optional API key. Defaults to AGENTLOOP_API_KEY."
    ),
) -> None:
    client = cli._remote_client(api_url, api_key)
    updated = client.update_finding_status(run_id, finding_id, status)
    cli.console.print_json(data=updated)


@cli.app.command("optimization-queue")
def optimization_queue(
    project_id: str | None = None,
    json_out: Path | None = typer.Option(None),
    sort_by: RankingSort = typer.Option(
        RankingSort.PRIORITY, help="Investigation ranking dimension."
    ),
) -> None:
    db = get_store()
    queue = sort_ranked(db.optimization_queue(project_id=project_id), sort_by.value)
    payload = {"project_id": project_id, "sort_by": sort_by.value, "queue": queue}
    if json_out is not None:
        cli._write_json(json_out, payload)
        cli.console.print(f"Wrote optimization queue JSON to {json_out}")
    table = Table(title="AgentLoop Optimization Queue")
    table.add_column("Priority / readiness")
    table.add_column("Severity")
    table.add_column("Type")
    table.add_column("Title")
    table.add_column("Runs")
    table.add_column("Patchable")
    table.add_column("Savings")
    for item in queue:
        table.add_row(
            f"{item['ranking']['priority_rank']} / {item['ranking']['status']}",
            str(item["severity"]),
            str(item["type"]),
            str(item["title"]),
            str(item["run_count"]),
            str(item["patchable_count"]),
            f"{format_duration_ms(item['estimated_latency_savings_ms'])} / "
            f"{format_cost_usd(item.get('estimated_cost_savings_usd'))}",
        )
    cli.console.print(table)

    for item in queue:
        cli.console.print(
            f"{item['title']}: "
            + (", ".join(item["ranking"]["reasons"]) or "Required ranking inputs are complete."),
            markup=False,
        )


@cli.app.command("github-issue-drafts")
def github_issue_drafts(
    project_id: str | None = None,
    out: Path = Path("runs/agentloop_issue_drafts.md"),
    json_out: Path | None = typer.Option(None),
    limit: int = typer.Option(5, min=1, max=20),
) -> None:
    db = get_store()
    drafts = build_issue_drafts(db.optimization_queue(project_id=project_id), limit=limit)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(issue_drafts_to_markdown(drafts), encoding="utf-8")
    if json_out is not None:
        cli._write_json(json_out, {"project_id": project_id, "issue_drafts": drafts})
        cli.console.print(f"Wrote GitHub issue draft JSON to {json_out}")
    cli.console.print(f"Wrote GitHub issue drafts to {out}")


@cli.app.command("usage-summary")
def usage_summary(project_id: str | None = None) -> None:
    db = get_store()
    summary = db.usage_summary(project_id=project_id)
    table = Table(title="AgentLoop Usage Summary")
    table.add_column("Metric")
    table.add_column("Value")
    for key, value in summary.items():
        rendered = (
            format_cost_usd(value, summary.get("cost_status", "complete"))
            if key == "estimated_cost_usd"
            else str(value)
        )
        table.add_row(key, rendered)
    cli.console.print(table)
