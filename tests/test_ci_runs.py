from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentloop.ci_runs import CIRunNotFoundError, ci_run_pair, list_ci_runs
from agentloop.store import SQLiteTraceStore
from agentloop.tracer import AgentTrace


def _upload(store, project, workflow_run, condition, *, commit="abc123", name=None):
    trace = AgentTrace(
        name=name or f"{workflow_run}-{condition}",
        metadata={
            "labels": {
                "workflow_run": workflow_run,
                "condition": condition,
                "commit": commit,
                "ref": "main",
                "repository": "team/app",
            }
        },
    )
    trace.finish()
    store.save_trace(trace, project_id=project)
    # Storage times have one-second resolution; give each upload its own second.
    _upload.clock += 1
    with store._connect() as conn:
        conn.execute(
            "UPDATE traces SET created_at = ? WHERE run_id = ?",
            (
                f"2026-11-24 10:{_upload.clock // 60:02d}:{_upload.clock % 60:02d}",
                trace.run_id,
            ),
        )
    return trace


_upload.clock = 0


@pytest.fixture
def store(tmp_path):
    return SQLiteTraceStore(path=str(tmp_path / "ci.db"))


def test_runs_are_grouped_with_their_newest_baseline_and_candidate(store):
    _upload(store, "team", "101", "baseline", name="old-baseline")
    _upload(store, "team", "101", "candidate")
    newest = _upload(store, "team", "101", "baseline", name="rerun-baseline")
    _upload(store, "team", "102", "baseline", commit="def456")
    _upload(store, "other", "101", "candidate")
    unlabelled = AgentTrace(name="manual")
    unlabelled.finish()
    store.save_trace(unlabelled, project_id="team")

    runs = list_ci_runs(store, "team")
    assert [run["workflow_run"] for run in runs] == ["102", "101"]
    run_102, run_101 = runs
    assert run_102["comparable"] is False and run_102["candidate_run_id"] is None
    assert run_102["commit"] == "def456"
    assert run_101["comparable"] is True
    assert run_101["baseline_run_id"] == newest.run_id
    assert run_101["traces"] == 3
    assert run_101["repository"] == "team/app" and run_101["ref"] == "main"

    baseline, candidate = ci_run_pair(store, "team", "101")
    assert baseline.name == "rerun-baseline" and candidate.name == "101-candidate"


def test_incomplete_or_foreign_runs_cannot_be_compared(store):
    _upload(store, "team", "102", "baseline")
    _upload(store, "other", "103", "baseline")
    _upload(store, "other", "103", "candidate")
    with pytest.raises(CIRunNotFoundError, match="no candidate"):
        ci_run_pair(store, "team", "102")
    with pytest.raises(CIRunNotFoundError, match="no baseline or candidate"):
        ci_run_pair(store, "team", "103")
    with pytest.raises(ValueError, match="scan"):
        list_ci_runs(store, "team", scan=0)


def test_api_lists_runs_and_compares_one(monkeypatch, tmp_path):
    database = tmp_path / "api.db"
    monkeypatch.setenv("AGENTLOOP_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(database))
    monkeypatch.delenv("AGENTLOOP_REQUIRE_API_KEY", raising=False)
    store = SQLiteTraceStore(path=str(database))
    _upload(store, "default", "201", "baseline")
    _upload(store, "default", "201", "candidate")
    _upload(store, "default", "202", "baseline")

    from agentloop.server import app

    client = TestClient(app)
    runs = client.get("/v1/ci-runs").json()["runs"]
    assert [(run["workflow_run"], run["comparable"]) for run in runs] == [
        ("202", False),
        ("201", True),
    ]
    comparison = client.get("/v1/ci-runs/201/comparison").json()
    assert comparison["replay"]["gates"]["passed"] in (True, False)
    assert comparison["markdown"].startswith("#") or "AgentLoop" in comparison["markdown"]
    assert comparison["baseline_run_id"] != comparison["candidate_run_id"]
    missing = client.get("/v1/ci-runs/202/comparison")
    assert missing.status_code == 404 and "no candidate" in missing.json()["detail"]
    assert client.get("/v1/ci-runs", params={"scan": 0}).status_code == 422
