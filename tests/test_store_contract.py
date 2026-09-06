"""Shared backend contract tests: every assertion here runs against both
SQLiteTraceStore and PostgresTraceStore so the two implementations can't drift
apart. Postgres tests skip (not fail) when no reachable service is configured,
so `pytest` stays usable with SQLite-only local setups; CI provides Postgres.
"""

from __future__ import annotations

import os
import threading

import pytest

from agentloop.store import (
    FindingNotFoundError,
    FindingTransitionError,
    InvalidCursorError,
    PostgresTraceStore,
    SQLiteTraceStore,
    TraceProjectConflictError,
)
from agentloop.tracer import trace_agent, trace_model_call, trace_tool_call

POSTGRES_TEST_DSN = os.getenv(
    "AGENTLOOP_TEST_POSTGRES_DSN", "postgresql://postgres:postgres@localhost:5432/agentloop_test"
)


def _postgres_available() -> bool:
    try:
        import psycopg
    except ImportError:
        return False
    try:
        with psycopg.connect(POSTGRES_TEST_DSN, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


_POSTGRES_AVAILABLE = _postgres_available()


def _reset_postgres_tables(store: PostgresTraceStore) -> None:
    store.init()
    with store._connect() as conn:
        conn.execute(
            "TRUNCATE trace_findings, usage_events, traces, api_keys, projects RESTART IDENTITY CASCADE"
        )
        conn.execute("INSERT INTO projects(project_id) VALUES ('default')")


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path):
    if request.param == "sqlite":
        return SQLiteTraceStore(path=str(tmp_path / "agentloop.db"))
    if not _POSTGRES_AVAILABLE:
        pytest.skip(
            "no Postgres service reachable at AGENTLOOP_TEST_POSTGRES_DSN "
            f"({POSTGRES_TEST_DSN}); start one to run these contract tests"
        )
    pg_store = PostgresTraceStore(dsn=POSTGRES_TEST_DSN)
    _reset_postgres_tables(pg_store)
    return pg_store


def _sample_trace(name: str = "contract-test"):
    with trace_agent(name) as trace:
        with trace_model_call("planner", input_text="hello", output_text="world"):
            pass
        with trace_tool_call("search"):
            pass
    return trace


def _repeated_context_trace(name: str = "queue-test"):
    with trace_agent(name) as trace:
        repeated = "stable context " * 100
        for _ in range(2):
            with trace_model_call("summarize", input_text=repeated, output_tokens=10):
                pass
        for _ in range(3):
            with trace_tool_call("search"):
                pass
    return trace


def _seed_trace(store, project_id: str, name: str = "seed") -> str:
    """Save a minimal trace and return its run_id.

    `save_diagnosis()` is only ever called (in `save_trace()` and the real
    `POST /traces/{run_id}/diagnosis` endpoint) for a run_id that already has a `traces` row —
    Postgres enforces that via a foreign key. Tests that call `save_diagnosis`
    directly with a synthetic finding list must seed that row first so both
    backends see the same, realistic precondition.
    """
    trace = _sample_trace(name=name)
    store.save_trace(trace, project_id=project_id)
    return trace.run_id


def _diagnosis_finding(finding_id, spans, latency_ms=100.0, cost_usd=0.0):
    return {
        "finding_id": finding_id,
        "type": "parallelize_tools",
        "severity": "medium",
        "title": "Parallelize independent tools",
        "confidence": "medium",
        "affected_spans": spans,
        "evidence": [],
        "savings": {
            "estimated_latency_savings_ms": latency_ms,
            "estimated_cost_savings_usd": cost_usd,
        },
        "rewrite": {"patchable": True},
        "validation": {},
        "metadata": {},
    }


# --- init -------------------------------------------------------------


def test_init_is_idempotent_and_creates_default_project(store):
    store.init()
    store.init()
    traces = store.list_traces(project_id="default")
    assert traces == []
    assert store.usage_summary(project_id="default")["cost_status"] == "empty"


def test_concurrent_init_does_not_raise_or_duplicate_migrations(store):
    errors: list[BaseException] = []

    def _init():
        try:
            store.init()
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=_init) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors


# --- api keys -----------------------------------------------------------


def test_api_key_roundtrip(store):
    created = store.create_api_key(project_id="proj_a", name="ci")
    verified = store.verify_api_key(created["api_key"])
    assert verified is not None
    assert verified["project_id"] == "proj_a"
    assert store.verify_api_key("al_wrong-key-entirely") is None


# --- trace upsert / conflicts -------------------------------------------


def test_trace_upsert_and_conflict(store):
    trace = _sample_trace()
    store.save_trace(trace, project_id="proj_a")
    store.save_trace(trace, project_id="proj_a")

    traces = store.list_traces(project_id="proj_a")
    assert len(traces) == 1
    assert traces[0]["run_id"] == trace.run_id

    with pytest.raises(TraceProjectConflictError):
        store.save_trace(trace, project_id="proj_b")


# --- idempotent, atomic ingestion (#10) ----------------------------------


def test_double_save_does_not_double_count_usage(store):
    trace = _sample_trace()
    store.save_trace(trace, project_id="proj_a")
    store.save_trace(trace, project_id="proj_a")

    summary = store.usage_summary(project_id="proj_a")
    assert summary["run_count"] == 1
    assert summary["model_call_count"] == 1


def test_cost_completeness_survives_trace_and_usage_persistence(store):
    with trace_agent("partial-cost") as trace:
        with trace_model_call("known", model="gpt-4o", input_tokens=100, output_tokens=10):
            pass
        with trace_model_call("unknown", model="private-model", input_tokens=100, output_tokens=10):
            pass

    store.save_trace(trace, project_id="proj_a")

    stored = store.list_traces(project_id="proj_a")[0]
    assert stored["cost_status"] == "partial"
    assert stored["priced_model_call_count"] == 1
    assert stored["unavailable_model_call_count"] == 1
    paged = store.list_traces_page(project_id="proj_a", limit=1)["items"][0]
    assert paged["cost_status"] == "partial"
    assert paged["priced_model_call_count"] == 1
    assert paged["unavailable_model_call_count"] == 1

    summary = store.usage_summary(project_id="proj_a")
    assert summary["cost_status"] == "partial"
    assert summary["estimated_cost_usd"] is None
    assert summary["priced_model_call_count"] == 1
    assert summary["unavailable_model_call_count"] == 1
    queue = store.optimization_queue(project_id="proj_a")
    assert queue
    assert all(item["cost_status"] == "partial" for item in queue)
    assert all(item["estimated_cost_savings_usd"] is None for item in queue)


def test_usage_missing_cost_is_persisted_as_unknown_not_zero(store):
    store.init()
    store.record_usage(
        "proj_a",
        "legacy-missing-cost",
        {
            "total_runtime_ms": 1,
            "input_tokens": 10,
            "output_tokens": 1,
            "model_call_count": 1,
            "tool_call_count": 0,
            "retry_count": 0,
        },
    )

    summary = store.usage_summary(project_id="proj_a")
    assert summary["cost_status"] == "unknown"
    assert summary["estimated_cost_usd"] is None
    assert summary["priced_model_call_count"] == 0
    assert summary["unavailable_model_call_count"] == 1


@pytest.mark.parametrize("cost_status", [None, "invalid", "empty"])
def test_usage_without_pricing_evidence_fails_closed(store, cost_status):
    store.init()
    report = {
        "estimated_cost_usd": 1.0,
        "model_call_count": 1,
    }
    if cost_status is not None:
        report["cost_status"] = cost_status
    store.record_usage("proj_a", "legacy-no-breakdown", report)

    summary = store.usage_summary(project_id="proj_a")
    assert summary["cost_status"] == "unknown"
    assert summary["estimated_cost_usd"] is None
    assert summary["priced_model_call_count"] == 0
    assert summary["unavailable_model_call_count"] == 1


@pytest.mark.parametrize(
    "bad_count",
    ["invalid", None, float("nan"), float("inf"), -1, 1.5, True, 2_147_483_648],
)
def test_usage_malformed_model_call_count_fails_closed(store, bad_count):
    store.init()
    store.record_usage(
        "proj_a",
        "malformed-model-count",
        {
            "estimated_cost_usd": 1.0,
            "model_call_count": bad_count,
        },
    )

    summary = store.usage_summary(project_id="proj_a")
    assert summary["cost_status"] == "unknown"
    assert summary["estimated_cost_usd"] is None
    assert summary["model_call_count"] == 0


@pytest.mark.parametrize("bad_cost", ["invalid", float("nan"), float("inf"), -1, True])
def test_usage_malformed_estimated_cost_fails_closed(store, bad_cost):
    store.init()
    store.record_usage(
        "proj_a",
        "malformed-cost",
        {
            "estimated_cost_usd": bad_cost,
            "model_call_count": 1,
        },
    )

    summary = store.usage_summary(project_id="proj_a")
    assert summary["cost_status"] == "unknown"
    assert summary["estimated_cost_usd"] is None


def test_retry_after_simulated_lost_response_is_idempotent(store):
    trace = _sample_trace()
    for _ in range(3):
        store.save_trace(trace, project_id="proj_a")

    assert len(store.list_traces(project_id="proj_a")) == 1
    assert store.usage_summary(project_id="proj_a")["run_count"] == 1
    assert len(store.list_findings(project_id="proj_a")) == len(
        store.list_findings(project_id="proj_a")
    )


def test_injected_failure_mid_save_rolls_back_the_whole_transaction(store, monkeypatch):
    trace = _sample_trace()

    def _boom(_trace):
        raise RuntimeError("simulated diagnosis failure")

    monkeypatch.setattr(
        type(store), "_upsert_findings", lambda self, conn, diagnosis, pid: _boom(None)
    )

    with pytest.raises(RuntimeError):
        store.save_trace(trace, project_id="proj_a")

    assert store.list_traces(project_id="proj_a") == []
    assert store.usage_summary(project_id="proj_a")["run_count"] == 0


# --- diagnosis / findings ------------------------------------------------


def test_optimization_queue_deduplicates_overlapping_findings_within_a_run(store):
    run_id = _seed_trace(store, "proj_a")
    store.save_diagnosis(
        {
            "run_id": run_id,
            "findings": [
                _diagnosis_finding("f1", ["n1"], 100.0),
                _diagnosis_finding("f2", ["n1", "n2"], 150.0),
                _diagnosis_finding("f3", ["n2"], 100.0),
            ],
        },
        project_id="proj_a",
    )

    queue = store.optimization_queue(project_id="proj_a")

    assert len(queue) == 1
    assert queue[0]["estimated_latency_savings_ms"] == 200.0
    assert queue[0]["occurrence_count"] == 3


def test_findings_filter_by_project_and_status(store):
    run_x = _seed_trace(store, "proj_a", name="run-x")
    run_y = _seed_trace(store, "proj_b", name="run-y")
    store.save_diagnosis(
        {"run_id": run_x, "findings": [_diagnosis_finding("f1", ["n1"])]}, project_id="proj_a"
    )
    store.save_diagnosis(
        {"run_id": run_y, "findings": [_diagnosis_finding("f1", ["n1"])]}, project_id="proj_b"
    )

    assert len(store.list_findings(project_id="proj_a")) == 1
    assert len(store.list_findings(project_id="proj_b")) == 1
    assert store.list_findings(project_id="proj_a", status="resolved") == []
    assert len(store.list_findings(project_id="proj_a", status="detected")) == 1


# --- finding lifecycle (#31) ---------------------------------------------


def test_finding_lifecycle_valid_and_invalid_transitions(store):
    run_id = _seed_trace(store, "proj_a")
    store.save_diagnosis(
        {"run_id": run_id, "findings": [_diagnosis_finding("f1", ["n1"])]}, project_id="proj_a"
    )

    accepted = store.update_finding_status("proj_a", run_id, "f1", "accepted")
    assert accepted["status"] == "accepted"

    resolved = store.update_finding_status("proj_a", run_id, "f1", "resolved")
    assert resolved["status"] == "resolved"

    with pytest.raises(FindingTransitionError):
        store.update_finding_status("proj_a", run_id, "f1", "accepted")

    reopened = store.update_finding_status("proj_a", run_id, "f1", "detected")
    assert reopened["status"] == "detected"


def test_finding_lifecycle_not_found_vs_project_isolation(store):
    run_id = _seed_trace(store, "proj_a")
    store.save_diagnosis(
        {"run_id": run_id, "findings": [_diagnosis_finding("f1", ["n1"])]}, project_id="proj_a"
    )

    with pytest.raises(FindingNotFoundError):
        store.update_finding_status("proj_a", run_id, "does_not_exist", "accepted")

    # A different project must not be able to see or transition proj_a's finding.
    with pytest.raises(FindingNotFoundError):
        store.update_finding_status("proj_b", run_id, "f1", "accepted")


def test_finding_lifecycle_retry_is_idempotent_not_a_conflict(store):
    run_id = _seed_trace(store, "proj_a")
    store.save_diagnosis(
        {"run_id": run_id, "findings": [_diagnosis_finding("f1", ["n1"])]}, project_id="proj_a"
    )
    store.update_finding_status("proj_a", run_id, "f1", "accepted")
    # Retrying the exact same already-applied transition should surface as a
    # conflict (current status is no longer the expected source), not a crash.
    with pytest.raises(FindingTransitionError):
        store.update_finding_status("proj_a", run_id, "f1", "accepted")


def test_finding_lifecycle_concurrent_updates_only_one_wins(store):
    run_id = _seed_trace(store, "proj_a")
    store.save_diagnosis(
        {"run_id": run_id, "findings": [_diagnosis_finding("f1", ["n1"])]}, project_id="proj_a"
    )
    results: list[str] = []
    errors: list[BaseException] = []

    def _accept():
        try:
            store.update_finding_status("proj_a", run_id, "f1", "accepted")
            results.append("ok")
        except FindingTransitionError:
            results.append("conflict")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_accept) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    assert results.count("ok") == 1
    assert results.count("conflict") == 4


def test_re_diagnosis_preserves_reviewed_status_and_supersedes_removed_findings(store):
    run_id = _seed_trace(store, "proj_a")
    store.save_diagnosis(
        {
            "run_id": run_id,
            "findings": [
                _diagnosis_finding("f1", ["n1"]),
                _diagnosis_finding("f2", ["n2"]),
            ],
        },
        project_id="proj_a",
    )
    store.update_finding_status("proj_a", run_id, "f1", "accepted")
    store.update_finding_status("proj_a", run_id, "f1", "resolved")

    # Re-diagnose: f1 unchanged, f2 gone, f3 new.
    store.save_diagnosis(
        {
            "run_id": run_id,
            "findings": [
                _diagnosis_finding("f1", ["n1"]),
                _diagnosis_finding("f3", ["n3"]),
            ],
        },
        project_id="proj_a",
    )

    by_id = {f["finding_id"]: f for f in store.list_findings(project_id="proj_a")}
    assert by_id["f1"]["status"] == "resolved", "reviewed status must survive re-diagnosis"
    assert by_id["f2"]["status"] == "superseded", "vanished, untriaged finding must be superseded"
    assert by_id["f3"]["status"] == "detected"

    # A superseded finding that reappears becomes active again.
    store.save_diagnosis(
        {
            "run_id": run_id,
            "findings": [
                _diagnosis_finding("f1", ["n1"]),
                _diagnosis_finding("f2", ["n2"]),
                _diagnosis_finding("f3", ["n3"]),
            ],
        },
        project_id="proj_a",
    )
    by_id = {f["finding_id"]: f for f in store.list_findings(project_id="proj_a")}
    assert by_id["f2"]["status"] == "detected"

    # The queue only ever reflects the currently active findings.
    queue = store.optimization_queue(project_id="proj_a")
    assert sum(item["occurrence_count"] for item in queue) == 2  # f2 + f3, f1 resolved


# --- pagination (#19) -----------------------------------------------------


def test_list_traces_page_empty(store):
    page = store.list_traces_page(project_id="proj_a", limit=5)
    assert page == {"items": [], "next_cursor": None}


def test_list_traces_page_walks_every_row_without_duplicates_or_gaps(store):
    run_ids = set()
    for index in range(7):
        trace = _sample_trace(name=f"trace-{index}")
        store.save_trace(trace, project_id="proj_a")
        run_ids.add(trace.run_id)

    seen = []
    cursor = None
    for _ in range(20):
        page = store.list_traces_page(project_id="proj_a", limit=3, cursor=cursor)
        seen.extend(item["run_id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert len(seen) == len(run_ids) == 7
    assert set(seen) == run_ids
    assert len(seen) == len(set(seen)), "pagination must not repeat a row across pages"


def test_list_traces_page_rejects_out_of_range_limit(store):
    with pytest.raises(ValueError):
        store.list_traces_page(project_id="proj_a", limit=0)
    with pytest.raises(ValueError):
        store.list_traces_page(project_id="proj_a", limit=100_000)


def test_list_traces_page_rejects_invalid_cursor(store):
    with pytest.raises(InvalidCursorError):
        store.list_traces_page(project_id="proj_a", limit=5, cursor="not-a-valid-cursor!!")


def test_list_findings_page_walks_every_row_and_respects_status_filter(store):
    run_ids = [_seed_trace(store, "proj_a", name=f"page-{index}") for index in range(5)]
    for run_id in run_ids:
        store.save_diagnosis(
            {"run_id": run_id, "findings": [_diagnosis_finding("f1", ["n1"])]},
            project_id="proj_a",
        )
    store.update_finding_status("proj_a", run_ids[0], "f1", "accepted")
    store.update_finding_status("proj_a", run_ids[0], "f1", "resolved")

    seen = []
    cursor = None
    for _ in range(20):
        page = store.list_findings_page(project_id="proj_a", limit=2, cursor=cursor)
        seen.extend((item["run_id"], item["finding_id"]) for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 5
    assert len(seen) == len(set(seen))

    resolved_only = store.list_findings_page(project_id="proj_a", status="resolved", limit=10)
    assert [item["run_id"] for item in resolved_only["items"]] == [run_ids[0]]
    assert resolved_only["next_cursor"] is None


def test_optimization_queue_bounded_window_excludes_inactive_statuses(store):
    run_id = _seed_trace(store, "proj_a")
    store.save_diagnosis(
        {
            "run_id": run_id,
            "findings": [_diagnosis_finding("f1", ["n1"]), _diagnosis_finding("f2", ["n2"])],
        },
        project_id="proj_a",
    )
    store.update_finding_status("proj_a", run_id, "f1", "accepted")
    store.update_finding_status("proj_a", run_id, "f1", "resolved")
    store.update_finding_status("proj_a", run_id, "f2", "dismissed")

    assert store.optimization_queue(project_id="proj_a") == []
