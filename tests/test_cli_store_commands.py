from __future__ import annotations

from typer.testing import CliRunner

from agentloop.cli import app
from agentloop.store import SQLiteTraceStore
from agentloop.tracer import trace_agent, trace_model_call

runner = CliRunner()


def _seed_store(db_path, project_id: str = "cli-test") -> tuple[str, str]:
    """Save a trace that produces at least one finding; return (run_id, finding_id)."""
    store = SQLiteTraceStore(path=str(db_path))
    with trace_agent("cli-test") as trace:
        repeated = "stable context " * 100
        for _ in range(2):
            with trace_model_call("summarize", input_text=repeated, output_tokens=10):
                pass
    store.save_trace(trace, project_id=project_id)
    finding = store.list_findings(project_id=project_id)[0]
    return trace.run_id, finding["finding_id"]


def test_list_stored_traces_limit_prints_next_cursor(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "runs" / "agentloop.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteTraceStore(path=str(db_path))
    for index in range(3):
        with trace_agent(f"trace-{index}") as trace:
            with trace_model_call("call", input_tokens=1, output_tokens=1):
                pass
        store.save_trace(trace, project_id="cli-test")

    result = runner.invoke(app, ["list-stored-traces", "--project-id", "cli-test", "--limit", "2"])

    assert result.exit_code == 0
    assert "More results available. Next cursor:" in result.output


def test_list_stored_traces_without_limit_matches_previous_behavior(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "runs" / "agentloop.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteTraceStore(path=str(db_path))
    with trace_agent("trace-only") as trace:
        with trace_model_call("call", input_tokens=1, output_tokens=1):
            pass
    store.save_trace(trace, project_id="cli-test")

    result = runner.invoke(app, ["list-stored-traces", "--project-id", "cli-test"])

    assert result.exit_code == 0
    assert trace.run_id in result.output
    assert "Next cursor" not in result.output


def test_update_finding_status_command_transitions_and_rejects_invalid(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "runs" / "agentloop.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    run_id, finding_id = _seed_store(db_path)

    accept = runner.invoke(
        app,
        ["update-finding-status", run_id, finding_id, "accepted", "--project-id", "cli-test"],
    )
    resolve = runner.invoke(
        app,
        ["update-finding-status", run_id, finding_id, "resolved", "--project-id", "cli-test"],
    )
    invalid = runner.invoke(
        app,
        ["update-finding-status", run_id, finding_id, "accepted", "--project-id", "cli-test"],
    )
    not_found = runner.invoke(
        app,
        ["update-finding-status", run_id, "does-not-exist", "accepted", "--project-id", "cli-test"],
    )

    assert accept.exit_code == 0
    assert "accepted" in accept.output
    assert resolve.exit_code == 0
    assert "resolved" in resolve.output
    assert invalid.exit_code != 0
    assert not_found.exit_code != 0

    store = SQLiteTraceStore(path=str(db_path))
    finding = next(
        f for f in store.list_findings(project_id="cli-test") if f["finding_id"] == finding_id
    )
    assert finding["status"] == "resolved"
