"""The Optimization Queue page reuses its queue across reruns until something changes."""

from __future__ import annotations

from pathlib import Path

import pytest

st = pytest.importorskip("streamlit")
from dashboard_apptest import APPTEST_TIMEOUT  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from agentloop.store import SQLiteTraceStore  # noqa: E402
from agentloop.tracer import trace_agent, trace_model_call, trace_tool_call  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "dashboard" / "app.py"


def _trace_with_findings(name: str):
    with trace_agent(name) as trace:
        for _ in range(3):
            with trace_model_call("summarize", input_text="stable context " * 100, output_tokens=8):
                pass
        for _ in range(3):
            with trace_tool_call("search"):
                pass
    return trace


def test_queue_is_reused_across_reruns_and_rebuilt_after_changes(tmp_path, monkeypatch):
    st.cache_resource.clear()
    st.cache_data.clear()
    calls: list[str | None] = []
    real = SQLiteTraceStore.optimization_queue

    def counting(self, project_id=None):
        calls.append(project_id)
        return real(self, project_id)

    monkeypatch.setattr(SQLiteTraceStore, "optimization_queue", counting)
    try:
        path = tmp_path / "queue.db"
        monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(path))
        monkeypatch.setenv("AGENTLOOP_DASHBOARD_RUNS_DIR", str(tmp_path / "runs"))
        monkeypatch.delenv("AGENTLOOP_DASHBOARD_REQUIRE_KEY", raising=False)
        store = SQLiteTraceStore(str(path))
        for index in range(2):
            store.save_trace(_trace_with_findings(f"agent-{index}"))

        app = AppTest.from_file(str(APP), default_timeout=APPTEST_TIMEOUT)
        app.run()
        app.sidebar.radio[0].set_value("Optimization Queue").run()
        assert not app.exception
        assert len(calls) == 1
        assert any("Queue computed at" in caption.value for caption in app.caption)

        # Re-sorting reruns the page but reuses the cached queue.
        sort = next(box for box in app.selectbox if box.label == "Sort investigations by")
        sort.set_value(sort.options[-1]).run()
        assert not app.exception
        assert len(calls) == 1

        # A status change made here rebuilds the queue.
        status = next(box for box in app.selectbox if box.label == "New status")
        status.set_value(status.options[0]).run()
        next(button for button in app.button if button.label == "Apply transition").click().run()
        assert not app.exception
        assert len(calls) == 2

        # So does the Refresh button, for changes made elsewhere.
        next(button for button in app.button if button.label == "Refresh").click().run()
        assert not app.exception
        assert len(calls) == 3
    finally:
        st.cache_resource.clear()
        st.cache_data.clear()
