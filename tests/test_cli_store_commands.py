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


def test_list_and_revoke_api_keys_from_the_cli(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "keys.db"
    monkeypatch.setenv("AGENTLOOP_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(db_path))
    store = SQLiteTraceStore(path=str(db_path))
    created = store.create_api_key(project_id="acme", name="ci")

    listed = runner.invoke(app, ["list-api-keys", "--project-id", "acme"])
    assert listed.exit_code == 0, listed.output
    assert created["prefix"] in listed.output
    assert created["api_key"] not in listed.output

    missing = runner.invoke(
        app, ["revoke-api-key", "--project-id", "other", "--prefix", created["prefix"]]
    )
    assert missing.exit_code == 1
    assert store.verify_api_key(created["api_key"]) is not None

    bad = runner.invoke(app, ["revoke-api-key", "--project-id", "acme", "--prefix", "al_"])
    assert bad.exit_code == 2

    revoked = runner.invoke(
        app, ["revoke-api-key", "--project-id", "acme", "--prefix", created["prefix"]]
    )
    assert revoked.exit_code == 0, revoked.output
    assert "Revoked 1" in revoked.output
    assert store.verify_api_key(created["api_key"]) is None


def test_delete_and_purge_traces_from_the_cli(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "retention.db"
    monkeypatch.setenv("AGENTLOOP_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(db_path))
    monkeypatch.delenv("AGENTLOOP_RETENTION_DAYS", raising=False)
    run_id, _ = _seed_store(db_path, project_id="acme")
    store = SQLiteTraceStore(path=str(db_path))

    assert runner.invoke(app, ["delete-trace", run_id, "--project-id", "other"]).exit_code == 1
    deleted = runner.invoke(app, ["delete-trace", run_id, "--project-id", "acme"])
    assert deleted.exit_code == 0, deleted.output
    assert store.get_trace(run_id, project_id="acme") is None

    unconfigured = runner.invoke(app, ["purge-traces"])
    assert unconfigured.exit_code == 0
    assert "not configured" in unconfigured.output

    old_run, _ = _seed_store(db_path, project_id="acme")
    with store._connect() as conn:
        conn.execute(
            "UPDATE traces SET created_at = '2000-01-01 00:00:00' WHERE run_id = ?", (old_run,)
        )
    monkeypatch.setenv("AGENTLOOP_RETENTION_DAYS", "30")
    purged = runner.invoke(app, ["purge-traces"])
    assert purged.exit_code == 0, purged.output
    assert "Deleted 1 trace(s) older than 30 days in all projects" in purged.output
    assert store.get_trace(old_run, project_id="acme") is None

    monkeypatch.setenv("AGENTLOOP_RETENTION_DAYS", "soon")
    assert runner.invoke(app, ["purge-traces"]).exit_code == 2


def test_list_stored_traces_filters_by_label(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "labels.db"
    monkeypatch.setenv("AGENTLOOP_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(db_path))
    store = SQLiteTraceStore(path=str(db_path))
    for name, commit in (("first", "abc"), ("second", "def")):
        with trace_agent(name) as trace:
            trace.metadata["labels"] = {"commit": commit}
        store.save_trace(trace, project_id="acme")

    result = runner.invoke(
        app, ["list-stored-traces", "--project-id", "acme", "--label", "commit=abc"]
    )
    assert result.exit_code == 0, result.output
    assert "first" in result.output and "second" not in result.output
    assert "commit=abc" in result.output
    bad = runner.invoke(app, ["list-stored-traces", "--label", "no-equals"])
    assert bad.exit_code == 2


def test_audit_log_command_shows_cli_actions(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "audit.db"
    monkeypatch.setenv("AGENTLOOP_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(db_path))
    assert runner.invoke(app, ["create-api-key", "--project-id", "acme"]).exit_code == 0
    result = runner.invoke(app, ["audit-log", "--project-id", "acme"])
    assert result.exit_code == 0, result.output
    assert '"api_key.created"' in result.output and '"cli"' in result.output
