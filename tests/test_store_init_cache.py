"""Stores check migrations once instead of on every call."""

from __future__ import annotations

import gc

from fastapi.testclient import TestClient

from agentloop import store as store_module
from agentloop.findings import build_diagnosis
from agentloop.store import SQLiteTraceStore
from agentloop.tracer import trace_agent, trace_model_call, trace_tool_call


def _trace():
    with trace_agent("init-cache") as trace:
        for _ in range(2):
            with trace_model_call("plan", input_text="stable context " * 80, output_tokens=5):
                pass
        with trace_tool_call("search"):
            pass
    return trace


def _count_migrations(monkeypatch) -> list[int]:
    calls: list[int] = []
    real = store_module.apply_sqlite_migrations

    def counting(conn):
        calls.append(1)
        return real(conn)

    monkeypatch.setattr(store_module, "apply_sqlite_migrations", counting)
    return calls


def test_store_methods_check_migrations_once(tmp_path, monkeypatch):
    calls = _count_migrations(monkeypatch)
    store = SQLiteTraceStore(str(tmp_path / "agentloop.db"))

    store.save_trace(_trace(), project_id="p")
    store.save_trace(_trace(), project_id="p")
    assert len(store.list_traces(project_id="p")) == 2
    store.usage_summary(project_id="p")
    store.optimization_queue(project_id="p")
    assert len(calls) == 1

    # An explicit init() still applies migrations.
    store.init()
    assert len(calls) == 2


def test_a_deleted_sqlite_file_is_recreated(tmp_path, monkeypatch):
    path = tmp_path / "agentloop.db"
    store = SQLiteTraceStore(str(path))
    store.save_trace(_trace(), project_id="p")

    gc.collect()  # sqlite3 connections close when collected; Windows locks open files
    path.unlink()

    store.save_trace(_trace(), project_id="p")
    assert len(store.list_traces(project_id="p")) == 1


def test_the_api_reuses_one_store_per_configuration(tmp_path, monkeypatch):
    from agentloop import server

    calls = _count_migrations(monkeypatch)
    monkeypatch.setenv("AGENTLOOP_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(tmp_path / "first.db"))
    monkeypatch.delenv("AGENTLOOP_REQUIRE_API_KEY", raising=False)
    client = TestClient(server.app)

    for _ in range(3):
        assert client.get("/v1/traces").status_code == 200
    assert len(calls) == 1

    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(tmp_path / "second.db"))
    assert client.get("/v1/traces").json()["traces"] == []
    assert len(calls) == 2
    assert (tmp_path / "second.db").exists()


def test_diagnosis_from_a_prebuilt_report_matches(tmp_path):
    trace = _trace()
    assert build_diagnosis(trace, report=trace.report()) == build_diagnosis(trace)
