import sqlite3

from agentloop.store import SQLiteTraceStore, hash_api_key, verify_api_key_hash
from agentloop.tracer import trace_agent, trace_model_call, trace_tool_call


def _sample_trace():
    with trace_agent("store-test") as trace:
        with trace_model_call("planner", input_text="hello", output_text="world"):
            pass
        with trace_tool_call("search"):
            pass
    return trace


def test_sqlite_store_roundtrip(tmp_path):
    store = SQLiteTraceStore(path=str(tmp_path / "agentloop.db"))
    trace = _sample_trace()

    store.save_trace(trace, project_id="proj_a")
    traces = store.list_traces(project_id="proj_a")

    assert len(traces) == 1
    assert traces[0]["run_id"] == trace.run_id

    loaded = store.get_trace(trace.run_id, project_id="proj_a")
    assert loaded is not None
    assert loaded.run_id == trace.run_id
    assert loaded.name == "store-test"
    assert loaded.report()["model_call_count"] == 1


def test_sqlite_api_key_verification(tmp_path):
    database_path = tmp_path / "agentloop.db"
    store = SQLiteTraceStore(path=str(database_path))
    created = store.create_api_key(project_id="proj_a", name="ci")

    verified = store.verify_api_key(created["api_key"])

    assert verified is not None
    assert verified["project_id"] == "proj_a"
    assert verified["name"] == "ci"
    assert store.verify_api_key("wrong") is None
    with sqlite3.connect(database_path) as connection:
        stored_hash = connection.execute("SELECT key_hash FROM api_keys").fetchone()[0]
    assert stored_hash.startswith("scrypt$v1$")
    assert created["api_key"] not in stored_hash


def test_api_key_hash_uses_unique_salts_and_rejects_malformed_hashes():
    api_key = "al_test-key-with-enough-random-looking-material"
    first_hash = hash_api_key(api_key)
    second_hash = hash_api_key(api_key)

    assert first_hash != second_hash
    assert verify_api_key_hash(api_key, first_hash) is True
    assert verify_api_key_hash("al_wrong-key", first_hash) is False
    assert verify_api_key_hash(api_key, "not-a-supported-hash") is False


def test_sqlite_usage_summary(tmp_path):
    store = SQLiteTraceStore(path=str(tmp_path / "agentloop.db"))
    trace = _sample_trace()
    store.save_trace(trace, project_id="proj_a")

    summary = store.usage_summary(project_id="proj_a")

    assert summary["project_id"] == "proj_a"
    assert summary["run_count"] == 1
    assert summary["model_call_count"] == 1
    assert summary["tool_call_count"] == 1


def _diagnosis_finding(finding_id, spans, latency_ms, cost_usd=0.0):
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


def test_optimization_queue_deduplicates_overlapping_findings_within_a_run(tmp_path):
    store = SQLiteTraceStore(path=str(tmp_path / "agentloop.db"))
    # f1=[n1]/100 and f3=[n2]/100 are compatible; f2=[n1, n2]/150 overlaps
    # both. A realizable fix applies f1+f3 for 200 ms — not the raw sum of 350.
    store.save_diagnosis(
        {
            "run_id": "run_overlap",
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
    item = queue[0]
    assert item["estimated_latency_savings_ms"] == 200.0
    assert item["occurrence_count"] == 3
    # priority_score consumes the deduplicated savings, not the raw sum:
    # 500 (medium) + 100 (patchable) + 200/100 + 3*25 = 677.
    assert item["priority_score"] == 677.0


def test_optimization_queue_sums_deduplicated_savings_across_runs(tmp_path):
    store = SQLiteTraceStore(path=str(tmp_path / "agentloop.db"))
    for run_id in ("run_one", "run_two"):
        store.save_diagnosis(
            {
                "run_id": run_id,
                "findings": [
                    _diagnosis_finding(f"{run_id}_a", ["n1"], 100.0),
                    _diagnosis_finding(f"{run_id}_b", ["n1"], 60.0),
                ],
            },
            project_id="proj_a",
        )

    queue = store.optimization_queue(project_id="proj_a")

    assert len(queue) == 1
    item = queue[0]
    # Within each run the two same-span findings are alternatives (100 wins);
    # the two independent runs still add up.
    assert item["estimated_latency_savings_ms"] == 200.0
    assert item["run_count"] == 2
    assert item["occurrence_count"] == 4


def test_sqlite_persists_findings_and_optimization_queue(tmp_path):
    store = SQLiteTraceStore(path=str(tmp_path / "agentloop.db"))
    with trace_agent("queue-test") as trace:
        repeated = "stable context " * 100
        for _ in range(2):
            with trace_model_call("summarize", input_text=repeated, output_tokens=10):
                pass
        for _ in range(3):
            with trace_tool_call("search"):
                pass

    store.save_trace(trace, project_id="proj_a")

    findings = store.list_findings(project_id="proj_a")
    queue = store.optimization_queue(project_id="proj_a")

    assert findings
    assert any(finding["type"] == "cache_context" for finding in findings)
    assert queue
    assert queue[0]["priority_score"] > 0
    assert queue[0]["run_count"] == 1
    assert "quality_risk" in queue[0]
    assert "requires_scorer" in queue[0]
