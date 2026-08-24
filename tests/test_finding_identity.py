from __future__ import annotations

import os

import pytest

from agentloop.events import AgentEvent
from agentloop.findings import build_diagnosis
from agentloop.store import PostgresTraceStore, SQLiteTraceStore
from agentloop.tracer import AgentTrace

POSTGRES_TEST_DSN = os.getenv(
    "AGENTLOOP_TEST_POSTGRES_DSN", "postgresql://postgres:postgres@localhost:5432/agentloop_test"
)


def _trace(run_id: str, *, event_id: str, step_name: str, with_retry: bool = False) -> AgentTrace:
    trace = AgentTrace(
        name="finding-identity",
        run_id=run_id,
        started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:00:01+00:00",
        elapsed_ms=1000.0,
    )
    if with_retry:
        trace.add_event(
            AgentEvent(
                event_id="retry-fixed",
                run_id=run_id,
                event_type="retry",
                name="repair",
                started_at="2026-01-01T00:00:00+00:00",
                ended_at="2026-01-01T00:00:00.050000+00:00",
                duration_ms=50.0,
            )
        )
    trace.add_event(
        AgentEvent(
            event_id=event_id,
            run_id=run_id,
            event_type="model_call",
            name=step_name,
            started_at="2026-01-01T00:00:00.100000+00:00",
            ended_at="2026-01-01T00:00:00.200000+00:00",
            duration_ms=100.0,
            model="gpt-4.1",
            input_tokens=100,
            output_tokens=20,
        )
    )
    return trace


def _route_finding(trace: AgentTrace) -> dict:
    return next(
        finding
        for finding in build_diagnosis(trace)["findings"]
        if finding["type"] == "route_to_smaller_model"
    )


def test_finding_id_survives_unrelated_card_insertion() -> None:
    run_id = "run-stable"
    original = _route_finding(_trace(run_id, event_id="event-a", step_name="plan-a"))
    reordered = _route_finding(
        _trace(run_id, event_id="event-a", step_name="plan-a", with_retry=True)
    )

    assert original["finding_id"] == reordered["finding_id"]
    assert original["finding_id"].startswith("al_route_to_smaller_model_")
    assert original["metadata"]["identity"] == "content-v1"


def test_changed_finding_evidence_gets_a_new_id() -> None:
    run_id = "run-stable"
    before = _route_finding(_trace(run_id, event_id="event-a", step_name="plan-a"))
    after = _route_finding(_trace(run_id, event_id="event-b", step_name="plan-b"))

    assert before["finding_id"] != after["finding_id"]


def _postgres_store() -> PostgresTraceStore | None:
    try:
        import psycopg
    except ImportError:
        return None
    try:
        with psycopg.connect(POSTGRES_TEST_DSN, connect_timeout=2):
            pass
    except psycopg.OperationalError:
        return None
    store = PostgresTraceStore(dsn=POSTGRES_TEST_DSN)
    store.init()
    with store._connect() as conn:
        conn.execute(
            "TRUNCATE trace_findings, usage_events, traces, api_keys, projects RESTART IDENTITY CASCADE"
        )
        conn.execute("INSERT INTO projects(project_id) VALUES ('default')")
    return store


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path):
    if request.param == "sqlite":
        return SQLiteTraceStore(path=str(tmp_path / "agentloop.db"))
    store = _postgres_store()
    if store is None:
        pytest.skip("Postgres is not available for finding identity contract tests")
    return store


def test_review_status_does_not_transfer_to_changed_finding(store) -> None:
    run_id = "run-stable"
    first_trace = _trace(run_id, event_id="event-a", step_name="plan-a")
    first = _route_finding(first_trace)
    store.save_trace(first_trace, project_id="proj")
    store.update_finding_status("proj", run_id, first["finding_id"], "accepted")
    store.update_finding_status("proj", run_id, first["finding_id"], "resolved")

    changed_trace = _trace(run_id, event_id="event-b", step_name="plan-b")
    changed = _route_finding(changed_trace)
    store.save_trace(changed_trace, project_id="proj")

    rows = store.list_findings(project_id="proj")
    by_id = {row["finding_id"]: row for row in rows}
    assert by_id[first["finding_id"]]["status"] == "resolved"
    assert by_id[changed["finding_id"]]["status"] == "detected"


def test_unchanged_finding_preserves_review_status(store) -> None:
    run_id = "run-stable"
    trace = _trace(run_id, event_id="event-a", step_name="plan-a")
    finding = _route_finding(trace)
    store.save_trace(trace, project_id="proj")
    store.update_finding_status("proj", run_id, finding["finding_id"], "accepted")

    store.save_trace(trace, project_id="proj")

    row = next(
        item
        for item in store.list_findings(project_id="proj")
        if item["finding_id"] == finding["finding_id"]
    )
    assert row["status"] == "accepted"
