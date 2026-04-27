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
