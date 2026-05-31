from agentloop.store import SQLiteTraceStore
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
    store = SQLiteTraceStore(path=str(tmp_path / "agentloop.db"))
    created = store.create_api_key(project_id="proj_a", name="ci")

    verified = store.verify_api_key(created["api_key"])

    assert verified is not None
    assert verified["project_id"] == "proj_a"
    assert verified["name"] == "ci"
    assert store.verify_api_key("wrong") is None


def test_sqlite_usage_summary(tmp_path):
    store = SQLiteTraceStore(path=str(tmp_path / "agentloop.db"))
    trace = _sample_trace()
    store.save_trace(trace, project_id="proj_a")

    summary = store.usage_summary(project_id="proj_a")

    assert summary["project_id"] == "proj_a"
    assert summary["run_count"] == 1
    assert summary["model_call_count"] == 1
    assert summary["tool_call_count"] == 1


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
