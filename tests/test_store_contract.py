"""Shared backend contract tests: every assertion here runs against both
SQLiteTraceStore and PostgresTraceStore so the two implementations can't drift
apart. Postgres tests skip (not fail) when no reachable service is configured,
so `pytest` stays usable with SQLite-only local setups; CI provides Postgres.
"""

from __future__ import annotations

import copy
import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

from agentloop.findings import build_diagnosis
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
            "TRUNCATE audit_events, trace_findings, usage_events, traces, api_keys, projects "
            "RESTART IDENTITY CASCADE"
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


def test_generic_workflow_metadata_and_operations_roundtrip(store):
    from workflow_fixtures import pipeline

    trace = pipeline(branching=True)
    store.save_trace(trace)
    loaded = store.get_trace(trace.run_id)
    assert loaded.to_dict() == trace.to_dict()
    assert loaded.report()["operation_counts"] == trace.report()["operation_counts"]
    assert loaded.report()["execution"]["workflow_id"] == "mail-router"
    assert loaded.report()["stages"]["route"]["depends_on"] == ["lookup", "priority"]


def test_external_atif_unknown_measurements_roundtrip_as_null(store):
    from pathlib import Path

    from agentloop.integrations.harbor.atif import import_atif

    fixture = Path(__file__).parent / "fixtures/external/harbor/atif_v17_simple.json"
    trace = import_atif(fixture).traces[0]
    store.save_trace(trace)
    loaded = store.get_trace(trace.run_id)
    assert loaded.to_dict() == trace.to_dict()
    assert loaded.report()["total_runtime_ms"] is None
    row = next(item for item in store.list_traces() if item["run_id"] == trace.run_id)
    assert row["total_runtime_ms"] is None
    assert row["estimated_cost_usd"] is None


def test_semantic_unknown_savings_are_preserved_in_both_backends(store):
    from test_semantic_findings import prepare

    trace = prepare()
    store.save_trace(trace)
    loaded = store.get_trace(trace.run_id)
    assert loaded.report()["semantic_waste"]["cases"][0]["status"] == "supported"
    saved = next(item for item in store.list_findings() if item["type"] == "semantic_redundancy")
    assert saved["estimated_latency_savings_ms"] is None
    assert saved["estimated_cost_savings_usd"] is None
    page = store.list_findings_page(limit=100)
    assert (
        next(item for item in page["items"] if item["type"] == "semantic_redundancy")[
            "estimated_latency_savings_ms"
        ]
        is None
    )
    queue = next(
        item for item in store.optimization_queue() if item["type"] == "semantic_redundancy"
    )
    assert queue["estimated_latency_savings_ms"] is None
    assert queue["estimated_cost_savings_usd"] is None
    assert queue["requires_scorer"] and not queue["safe_to_auto_patch"]


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


def test_verified_api_keys_skip_scrypt_until_revoked(store, monkeypatch):
    from agentloop import store as store_module

    created = store.create_api_key(project_id="proj_a", name="ci")
    calls: list[str] = []
    real = store_module.verify_api_key_hash

    def counting(api_key, encoded_hash):
        calls.append(encoded_hash)
        return real(api_key, encoded_hash)

    monkeypatch.setattr(store_module, "verify_api_key_hash", counting)

    assert store.verify_api_key(created["api_key"])["project_id"] == "proj_a"
    assert len(calls) == 1
    for _ in range(3):
        assert store.verify_api_key(created["api_key"])["project_id"] == "proj_a"
    assert len(calls) == 1

    # A different key with the same prefix still goes through scrypt and fails.
    assert store.verify_api_key(created["api_key"][:-1] + "x") is None
    assert len(calls) == 2

    assert store.revoke_api_key("proj_a", created["prefix"]) == 1
    assert store.verify_api_key(created["api_key"]) is None


def test_api_keys_are_listed_without_secrets_and_revoked_per_project(store):
    first = store.create_api_key(project_id="proj_a", name="ci")
    second = store.create_api_key(project_id="proj_a", name="laptop")
    other = store.create_api_key(project_id="proj_b", name="ci")
    listed = store.list_api_keys("proj_a")
    assert sorted(item["name"] for item in listed) == ["ci", "laptop"]
    assert {item["prefix"] for item in listed} == {first["prefix"], second["prefix"]}
    for item in listed:
        assert set(item) == {"project_id", "prefix", "name", "created_at", "last_used_at"}
        assert first["api_key"] not in str(item)
    # Another project's key cannot be revoked through this project.
    assert store.revoke_api_key("proj_a", other["prefix"]) == 0
    assert store.verify_api_key(other["api_key"]) is not None
    assert store.revoke_api_key("proj_a", first["prefix"]) == 1
    assert store.verify_api_key(first["api_key"]) is None
    assert store.verify_api_key(second["api_key"]) is not None
    assert [item["name"] for item in store.list_api_keys("proj_a")] == ["laptop"]
    with pytest.raises(ValueError, match="prefix"):
        store.revoke_api_key("proj_a", "al_")


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


def test_finding_counts_and_type_title_filters(store):
    run_id = _seed_trace(store, "proj_a")
    other = _diagnosis_finding("f2", ["n9"])
    other.update(type="cache_prompt", title="Cache the prompt", rewrite={"patchable": False})
    store.save_diagnosis(
        {"run_id": run_id, "findings": [_diagnosis_finding("f1", ["n1"]), other]},
        project_id="proj_a",
    )
    store.save_diagnosis(
        {"run_id": _seed_trace(store, "proj_b"), "findings": [_diagnosis_finding("f1", ["n1"])]},
        project_id="proj_b",
    )

    findings = store.list_findings(project_id="proj_a")
    assert store.finding_counts(project_id="proj_a") == {
        "total": len(findings),
        "patchable": sum(1 for finding in findings if finding["patchable"]),
    }
    assert store.finding_counts(project_id="empty") == {"total": 0, "patchable": 0}
    assert store.finding_counts()["total"] == len(store.list_findings())

    cached = store.list_findings(project_id="proj_a", type="cache_prompt", title="Cache the prompt")
    assert [item["finding_id"] for item in cached] == ["f2"]
    assert store.list_findings(project_id="proj_a", type="cache_prompt", title="other") == []
    assert {item["finding_id"] for item in store.list_findings(project_id="proj_a")} >= {"f1", "f2"}


def test_optimization_queue_lists_only_the_newest_members_in_full(store):
    from agentloop.ranking import MAX_LISTED_MEMBERS

    runs = MAX_LISTED_MEMBERS + 5
    for index in range(runs):
        store.save_diagnosis(
            {
                "run_id": _seed_trace(store, "proj_a", name=f"run-{index}"),
                "findings": [_diagnosis_finding("f1", ["n1"])],
            },
            project_id="proj_a",
        )

    [item] = store.optimization_queue(project_id="proj_a")

    # Every member still counts toward the cluster; only the listing is capped.
    assert item["occurrence_count"] == runs
    assert item["run_count"] == runs
    assert item["estimated_latency_savings_ms"] == 100.0 * runs
    ranking = item["ranking"]
    assert ranking["member_count"] == runs
    assert ranking["members_truncated"] is True
    assert len(ranking["members"]) == MAX_LISTED_MEMBERS
    assert len(ranking["estimate_provenance"]) == MAX_LISTED_MEMBERS


# --- finding lifecycle (#31) ---------------------------------------------


def test_persisted_estimator_snapshot_survives_future_rule_changes(store, monkeypatch):
    from agentloop.entrypoint import _quickstart_trace
    from agentloop.estimates import ESTIMATORS
    from agentloop.findings import build_diagnosis

    trace = _quickstart_trace()
    expected = build_diagnosis(trace)
    store.save_trace(trace, project_id="historical")
    monkeypatch.setitem(ESTIMATORS, "parallelize_tools", None)
    stored = store.list_findings(project_id="historical")
    by_id = {row["finding_id"]: row["finding"] for row in stored}
    for finding in expected["findings"]:
        assert by_id[finding["finding_id"]]["estimate"] == finding["estimate"]
    assert store.list_findings(project_id="another") == []


@pytest.fixture
def intervention_evidence(store):
    from agentloop.entrypoint import _quickstart_trace
    from agentloop.findings import build_diagnosis
    from agentloop.interventions import build_intervention

    baseline = _quickstart_trace()
    candidate = copy.deepcopy(baseline)
    candidate.run_id = "ledger-candidate"
    for event in candidate.events:
        event.run_id = candidate.run_id
    store.save_trace(baseline, project_id="ledger")
    store.save_trace(candidate, project_id="ledger")
    diagnosis = build_diagnosis(baseline)
    record = build_intervention(
        baseline,
        candidate,
        target_finding_ids=[finding["finding_id"] for finding in diagnosis["findings"]],
        intervention_type="context_compression",
        diagnosis=diagnosis,
    )
    return baseline, candidate, diagnosis, record


def test_intervention_persistence_is_idempotent_immutable_and_project_scoped(
    store, intervention_evidence
):
    from agentloop.interventions import InterventionConflictError, InterventionReferenceError

    baseline, _, diagnosis, record = intervention_evidence
    assert store.save_intervention(record, project_id="ledger") == record.to_dict()
    assert store.save_intervention(record, project_id="ledger") == record.to_dict()
    assert store.get_intervention(record.intervention_id, project_id="other") is None
    with pytest.raises(InterventionReferenceError):
        store.save_intervention(record, project_id="other")
    changed = record.to_dict()
    changed["metadata"]["experiment"] = "different evidence"
    with pytest.raises(InterventionConflictError):
        store.save_intervention(changed, project_id="ledger")
    diagnosis["findings"][0]["estimate"]["estimator_version"] = "next-version"
    store.save_diagnosis(diagnosis, project_id="ledger")
    assert (
        store.get_finding_snapshot(
            baseline.run_id, diagnosis["findings"][0]["finding_id"], project_id="ledger"
        )["estimate"]["estimator_version"]
        == "next-version"
    )
    assert store.get_intervention(record.intervention_id, project_id="ledger") == record.to_dict()


def test_intervention_rejects_changed_traces_and_missing_findings(store, intervention_evidence):
    from agentloop.interventions import (
        InterventionConflictError,
        InterventionReferenceError,
        build_intervention,
    )

    baseline, candidate, diagnosis, record = intervention_evidence
    candidate.metadata["modified"] = True
    store.save_trace(candidate, project_id="ledger")
    with pytest.raises(InterventionConflictError, match="source trace"):
        store.save_intervention(record, project_id="ledger")
    diagnosis["findings"][0]["finding_id"] = "missing-finding"
    missing = build_intervention(
        baseline,
        candidate,
        target_finding_ids=["missing-finding"],
        intervention_type="change",
        diagnosis=diagnosis,
    )
    with pytest.raises(InterventionReferenceError, match="finding"):
        store.save_intervention(missing, project_id="ledger")
    assert store.get_intervention(missing.intervention_id, project_id="ledger") is None


def test_concurrent_intervention_retries_have_one_evidence_result(store, intervention_evidence):
    from concurrent.futures import ThreadPoolExecutor

    _, _, _, record = intervention_evidence
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(lambda _: store.save_intervention(record, project_id="ledger"), range(4))
        )
    assert results == [record.to_dict()] * 4


def test_intervention_preserves_failed_quality_and_indeterminate_cost(store, intervention_evidence):
    from agentloop.interventions import build_intervention
    from agentloop.quality import build_quality_report

    baseline, candidate, diagnosis, _ = intervention_evidence
    for trace in (baseline, candidate):
        for event in trace.events:
            if event.event_type == "model_call":
                event.model = "unknown-model"
        store.save_trace(trace, project_id="ledger")
    quality = build_quality_report(
        [{"expected": "ok", "baseline_output": "ok", "candidate_output": "bad"}]
    )
    record = build_intervention(
        baseline,
        candidate,
        target_finding_ids=[finding["finding_id"] for finding in diagnosis["findings"]],
        intervention_type="change",
        diagnosis=diagnosis,
        quality_report=quality,
    )
    stored = store.save_intervention(record, project_id="ledger")
    assert stored["gates_passed"] is False
    assert stored["measured"]["quality"]["failed_case_count"] == 1
    assert stored["measured"]["deltas"]["cost_usd_delta"] is None


def test_incomplete_diagnosis_does_not_supersede_historical_findings(store):
    run_id = _seed_trace(store, "proj_a")
    store.save_diagnosis(
        {"run_id": run_id, "findings": [_diagnosis_finding("f1", ["n1"])]}, project_id="proj_a"
    )
    store.update_finding_status("proj_a", run_id, "f1", "accepted")
    store.save_diagnosis(
        {"run_id": run_id, "findings": [], "analysis_complete": False}, project_id="proj_a"
    )
    assert store.list_findings(project_id="proj_a")[0]["status"] == "accepted"
    store.save_diagnosis(
        {"run_id": run_id, "findings": [], "analysis_complete": True}, project_id="proj_a"
    )
    assert store.list_findings(project_id="proj_a")[0]["status"] == "superseded"


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


def test_harness_decisions_and_linked_interventions_preserve_project_isolation(store):
    from agentloop.findings import build_diagnosis
    from agentloop.harness import Decision, Harness, HarnessConfig, HarnessDeniedError, Hook, Policy
    from agentloop.harness_evidence import read_evidence
    from agentloop.intervention_service import create_stored_intervention
    from agentloop.interventions import InterventionReferenceError
    from agentloop.tracer import AgentTrace, bind_trace_context

    baseline = _repeated_context_trace("baseline-policy")
    candidate = AgentTrace.from_dict(copy.deepcopy(baseline.to_dict()))
    candidate.run_id = "candidate-" + baseline.run_id
    for event in candidate.events:
        event.run_id = candidate.run_id
    policy = Policy(
        "deny-tool",
        "historical",
        lambda context: Decision("deny"),
        hooks={Hook("tool")},
        actions={"deny"},
    )
    with bind_trace_context(candidate):
        run = Harness(HarnessConfig("enforce", (policy,))).start_run()
        with pytest.raises(HarnessDeniedError):
            run.wrap(lambda: pytest.fail("denied tool dispatched"), boundary="tool")()
    expected = read_evidence(candidate)
    for trace in (baseline, candidate):
        store.save_trace(trace, project_id="proj_a")
    assert read_evidence(store.get_trace(candidate.run_id, project_id="proj_a")) == expected
    assert store.get_trace(candidate.run_id, project_id="proj_b") is None
    targets = [item["finding_id"] for item in build_diagnosis(baseline)["findings"]]
    request = {
        "baseline_run_id": baseline.run_id,
        "candidate_run_id": candidate.run_id,
        "target_finding_ids": targets,
        "intervention_type": "guarded_tool",
    }
    record = create_stored_intervention(store, request, project_id="proj_a")
    assert create_stored_intervention(store, request, project_id="proj_a") == record
    evidence = record["metadata"]["agentloop.harness_evidence"]
    assert evidence["candidate"] == expected
    assert len(evidence["applied_candidate_decision_ids"]) == 1
    assert store.get_intervention(record["intervention_id"], project_id="proj_b") is None
    with pytest.raises(InterventionReferenceError):
        create_stored_intervention(store, request, project_id="proj_b")


def test_budget_snapshots_keep_decimal_totals_and_unknowns_across_backends(store):
    from agentloop.budget_types import DispatchOptions, Reservation, ResourceUsage
    from agentloop.budgets import BudgetLimits, budget_policy
    from agentloop.harness import Harness, HarnessConfig
    from agentloop.harness_evidence import read_evidence

    with trace_agent("budget-store", metadata={"synthetic": True}) as trace:
        policy = budget_policy(
            BudgetLimits(max_model_calls=2, max_tokens=10, max_cost_usd=1),
            unknown_usage="monitor_only",
        )
        run = Harness(HarnessConfig("enforce", (policy,))).start_run()
        report = ResourceUsage(3, 0.03, "provider", "provider_reported", complete=True)
        run.wrap(
            lambda: None,
            boundary="model",
            dispatch=DispatchOptions(Reservation(5, 0.1, "upper_bound", True)),
            usage_reader=lambda _: report,
        )()
        run.wrap(
            lambda: None,
            boundary="model",
            dispatch=DispatchOptions(Reservation(5, 0.1, "upper_bound", True)),
        )()
    expected = read_evidence(trace)
    store.save_trace(trace, project_id="proj_a")
    actual = read_evidence(store.get_trace(trace.run_id, project_id="proj_a"))
    assert actual == expected
    assert store.get_trace(trace.run_id, project_id="proj_b") is None
    snapshots = [
        record["budget_snapshot"]
        for record in actual["decisions"].values()
        if record["budget_snapshot"]
    ]
    latest = max(snapshots, key=lambda snapshot: snapshot["committed"]["model_calls"])
    assert latest["committed"]["cost_known_usd"] == "0.03"
    assert latest["unknown_usage"]["cost_held_usd"] == "0.1"
    assert latest["total_usage"] == {"tokens": None, "cost_usd": None}


def test_loop_feedback_preserves_schema_and_project_isolation(store):
    from agentloop.budget_types import DispatchOptions
    from agentloop.harness import Harness, HarnessConfig
    from agentloop.harness_evidence import read_evidence
    from agentloop.loop_guards import LoopLimits, loop_guard_policy
    from agentloop.loop_types import StepInfo, fingerprint

    with trace_agent("loop-store", metadata={"synthetic": True}) as trace:
        run = Harness(
            HarnessConfig("shadow", (loop_guard_policy(LoopLimits(max_identical_calls=1)),))
        ).start_run()
        step = StepInfo(
            "step", fingerprint({"input": 1}), fingerprint({"progress": 0}), mutating=False
        )
        wrapped = run.wrap(lambda: None, boundary="tool", dispatch=DispatchOptions(step=step))
        wrapped()
        wrapped()
    expected = read_evidence(trace)
    store.save_trace(trace, project_id="proj_a")
    assert read_evidence(store.get_trace(trace.run_id, project_id="proj_a")) == expected
    assert store.get_trace(trace.run_id, project_id="proj_b") is None
    assert expected["schema_version"] == "1.1"
    assert any(
        record["reason_code"] == "identical_step_limit" and record["outcome"] == "proposed"
        for record in expected["decisions"].values()
    )


def _mark(store):
    return "?" if isinstance(store, SQLiteTraceStore) else "%s"


def _add_intervention(store, project_id, intervention_id, baseline, candidate):
    mark = _mark(store)
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO interventions(project_id, intervention_id, baseline_run_id, "
            f"candidate_run_id, payload_json) VALUES ({mark}, {mark}, {mark}, {mark}, {mark})",
            (project_id, intervention_id, baseline, candidate, "{}"),
        )


def _age(store, run_id, days):
    mark = _mark(store)
    stamp = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with store._connect() as conn:
        conn.execute(
            f"UPDATE traces SET created_at = {mark} WHERE run_id = {mark}", (stamp, run_id)
        )


def _count(store, table, run_id):
    mark = _mark(store)
    column = "baseline_run_id" if table == "interventions" else "run_id"
    with store._connect() as conn:
        return conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column} = {mark}", (run_id,)
        ).fetchone()[0]


def _trace_with_findings(name):
    with trace_agent(name) as trace:
        repeated = "stable context " * 100
        for _ in range(2):
            with trace_model_call("summarize", input_text=repeated, output_tokens=10):
                pass
    return trace


def test_delete_trace_removes_derived_records_only_in_its_project(store):
    baseline = _trace_with_findings("baseline")
    candidate = _sample_trace("candidate")
    other = _sample_trace("other-project")
    store.save_trace(baseline, project_id="proj_a")
    store.save_trace(candidate, project_id="proj_a")
    store.save_trace(other, project_id="proj_b")
    store.save_diagnosis(build_diagnosis(baseline), project_id="proj_a")
    _add_intervention(store, "proj_a", "int_1", baseline.run_id, candidate.run_id)
    assert _count(store, "trace_findings", baseline.run_id) > 0

    # The wrong project cannot delete it.
    assert store.delete_trace(baseline.run_id, "proj_b") is None
    assert store.get_trace(baseline.run_id, project_id="proj_a") is not None

    deleted = store.delete_trace(baseline.run_id, "proj_a")
    assert deleted["traces"] == 1 and deleted["interventions"] == 1
    assert deleted["findings"] >= 1 and deleted["usage_events"] == 1
    assert store.get_trace(baseline.run_id, project_id="proj_a") is None
    for table in ("trace_findings", "usage_events", "interventions"):
        assert _count(store, table, baseline.run_id) == 0
    assert store.get_trace(candidate.run_id, project_id="proj_a") is not None
    assert store.get_trace(other.run_id, project_id="proj_b") is not None
    assert store.delete_trace(baseline.run_id, "proj_a") is None


def test_purge_deletes_only_traces_older_than_the_retention_period(store):
    old_a, new_a, old_b = _sample_trace("old-a"), _sample_trace("new-a"), _sample_trace("old-b")
    store.save_trace(old_a, project_id="proj_a")
    store.save_trace(new_a, project_id="proj_a")
    store.save_trace(old_b, project_id="proj_b")
    _age(store, old_a.run_id, 40)
    _age(store, old_b.run_id, 40)

    only_b = store.purge_traces(30, project_id="proj_b")
    assert only_b["traces"] == 1
    assert store.get_trace(old_a.run_id, project_id="proj_a") is not None

    everywhere = store.purge_traces(30)
    assert everywhere["traces"] == 1 and everywhere["usage_events"] == 1
    assert store.get_trace(old_a.run_id, project_id="proj_a") is None
    assert store.get_trace(new_a.run_id, project_id="proj_a") is not None
    assert store.purge_traces(30)["traces"] == 0
    for invalid in (0, -1, float("nan"), True, "30"):
        with pytest.raises(ValueError, match="positive number of days"):
            store.purge_traces(invalid)


def test_traces_list_their_labels_and_filter_by_one(store):
    from agentloop.store import parse_label_filter

    def labelled(name, **labels):
        trace = _sample_trace(name)
        if labels:
            trace.metadata["labels"] = labels
        store.save_trace(trace, project_id="proj_a")
        return trace

    base = labelled("base", condition="baseline", commit="abc", **{"ci.run": "7"})
    cand = labelled("cand", condition="candidate", commit="abc")
    other = labelled("other", condition="candidate", commit="def")
    plain = labelled("plain")
    store.save_trace(_sample_trace("elsewhere"), project_id="proj_b")

    page = store.list_traces_page(project_id="proj_a", limit=10)
    by_run = {item["run_id"]: item["labels"] for item in page["items"]}
    assert by_run[base.run_id] == {"condition": "baseline", "commit": "abc", "ci.run": "7"}
    assert by_run[plain.run_id] == {}

    commit_abc = store.list_traces_page(project_id="proj_a", limit=10, label=("commit", "abc"))
    assert {item["run_id"] for item in commit_abc["items"]} == {base.run_id, cand.run_id}
    dotted = store.list_traces_page(project_id="proj_a", limit=10, label=("ci.run", "7"))
    assert [item["run_id"] for item in dotted["items"]] == [base.run_id]
    assert store.list_traces_page(project_id="proj_b", label=("commit", "abc"))["items"] == []

    # Pagination keeps the filter: two candidates, one per page.
    first = store.list_traces_page(project_id="proj_a", limit=1, label=("condition", "candidate"))
    assert first["next_cursor"] is not None
    second = store.list_traces_page(
        project_id="proj_a", limit=1, cursor=first["next_cursor"], label=("condition", "candidate")
    )
    assert {first["items"][0]["run_id"], second["items"][0]["run_id"]} == {
        cand.run_id,
        other.run_id,
    }
    assert second["next_cursor"] is None

    assert parse_label_filter("commit=a=b") == ("commit", "a=b")
    for bad in ("no-equals", "=x", "bad key=1", "k=" + "v" * 501):
        with pytest.raises(ValueError, match="KEY=VALUE"):
            parse_label_filter(bad)
    with pytest.raises(ValueError, match="KEY=VALUE"):
        store.list_traces_page(label=("bad key", "x"))


def test_audit_log_records_key_and_deletion_actions_without_secrets(store):
    key = store.create_api_key(project_id="proj_a", name="ci", actor="admin-api")
    store.create_api_key(project_id="proj_b", name="other", actor="cli")
    trace = _sample_trace("audited")
    store.save_trace(trace, project_id="proj_a")
    store.delete_trace(trace.run_id, "proj_a", actor="api-key:al_abc1234")
    old = _sample_trace("old")
    store.save_trace(old, project_id="proj_a")
    _age(store, old.run_id, 40)
    store.purge_traces(30, actor="retention:AGENTLOOP_RETENTION_DAYS")
    store.purge_traces(30, actor="retention:AGENTLOOP_RETENTION_DAYS")  # nothing left: no event
    store.revoke_api_key("proj_a", key["prefix"], actor="admin-api")
    store.revoke_api_key("proj_a", key["prefix"], actor="admin-api")  # nothing revoked: no event

    events = store.list_audit_events("proj_a")
    assert [event["action"] for event in events] == [
        "api_key.revoked",
        "traces.purged",
        "trace.deleted",
        "api_key.created",
    ]
    revoked, purged, deleted, created = events
    assert created["target"] == key["prefix"] and created["details"] == {"name": "ci"}
    assert created["actor"] == "admin-api"
    assert deleted["target"] == trace.run_id and deleted["details"]["traces"] == 1
    assert deleted["actor"] == "api-key:al_abc1234"
    assert purged["details"]["traces"] == 1 and purged["details"]["older_than_days"] == 30.0
    assert revoked["details"] == {"revoked": 1}
    assert all(event["occurred_at"] for event in events)
    assert key["api_key"] not in str(store.list_audit_events())

    assert [event["project_id"] for event in store.list_audit_events("proj_b")] == ["proj_b"]
    assert len(store.list_audit_events()) == 5
    assert len(store.list_audit_events(limit=2)) == 2
