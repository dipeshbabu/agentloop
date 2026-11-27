"""Demo data, the API server and the MCP server.

Split out of ``agentloop.cli`` (issue #407). Commands register on
``agentloop.cli.app``; shared helpers are looked up through ``agentloop.cli`` at
call time, so tests and callers that patch them there keep working.
"""

from __future__ import annotations

from pathlib import Path

import typer

from agentloop import cli


@cli.app.command()
def demo(
    kind: str = typer.Option("baseline", help="baseline, optimized, langgraph, or proof"),
) -> None:
    from agentloop.demo import run_baseline, run_langgraph_style, run_optimized, run_proof_pair

    if kind == "baseline":
        path = run_baseline()
    elif kind == "optimized":
        path = run_optimized()
    elif kind == "langgraph":
        path = run_langgraph_style()
    elif kind == "proof":
        baseline, candidate = run_proof_pair()
        cli.console.print(f"Wrote synthetic demo trace to {baseline}")
        cli.console.print(f"Wrote synthetic demo trace to {candidate}")
        return
    else:
        raise typer.BadParameter("kind must be 'baseline', 'optimized', 'langgraph', or 'proof'")
    cli.console.print(f"Wrote synthetic demo trace to {path}")


@cli.app.command("demo-all")
def demo_all() -> None:
    from agentloop.demo import run_baseline, run_langgraph_style, run_optimized, run_proof_pair

    base = run_baseline()
    opt = run_optimized()
    lg = run_langgraph_style()
    proof_base, proof_candidate = run_proof_pair()
    cli.console.print(f"Wrote synthetic demo trace to {base}")
    cli.console.print(f"Wrote synthetic demo trace to {opt}")
    cli.console.print(f"Wrote synthetic demo trace to {lg}")
    cli.console.print(f"Wrote synthetic demo trace to {proof_base}")
    cli.console.print(f"Wrote synthetic demo trace to {proof_candidate}")


@cli.app.command()
def server(host: str = "127.0.0.1", port: int = 8000, reload: bool = False) -> None:
    try:
        import uvicorn
    except ImportError as exc:
        raise typer.BadParameter(
            "Install server dependencies with: uv sync --locked --extra server"
        ) from exc
    uvicorn.run("agentloop.server:app", host=host, port=port, reload=reload)


@cli.app.command("mcp")
def mcp_command(
    root: Path = typer.Option(Path("."), help="Workspace root; every path stays inside it."),
    allow_execution: bool = typer.Option(
        False, help="Expose execute_candidate, which validates changes in a temporary worktree."
    ),
    include_text: bool = typer.Option(
        False, help="Include recorded model input and output text in tool results."
    ),
    timeout: float = typer.Option(60.0, help="Seconds allowed per tool call."),
) -> None:
    """Run a local MCP server over stdio for coding agents."""
    from agentloop.mcp_server import Server

    try:
        server_instance = Server(
            root, allow_execution=allow_execution, include_text=include_text, timeout_s=timeout
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    server_instance.serve()
