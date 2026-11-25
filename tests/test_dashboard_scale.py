"""The dashboard stays usable when a project has more runs than it lists."""

from __future__ import annotations

from pathlib import Path

import pytest

st = pytest.importorskip("streamlit")
from dashboard_apptest import APPTEST_TIMEOUT  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from agentloop.store import MAX_PAGE_SIZE, SQLiteTraceStore  # noqa: E402
from agentloop.tracer import trace_agent, trace_model_call  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "dashboard" / "app.py"


def _store_runs(store: SQLiteTraceStore, count: int) -> list[str]:
    run_ids = []
    for index in range(count):
        with trace_agent(f"agent-{index}") as trace:
            with trace_model_call("plan", input_tokens=10, output_tokens=5):
                pass
        store.save_trace(trace)
        run_ids.append(trace.run_id)
    return run_ids


def _text_input(app: AppTest, label: str):
    return next(widget for widget in app.text_input if widget.label == label)


def test_pages_list_the_newest_runs_and_open_older_ones_by_id(tmp_path, monkeypatch):
    st.cache_resource.clear()
    try:
        path = tmp_path / "scale.db"
        monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(path))
        monkeypatch.setenv("AGENTLOOP_DASHBOARD_RUNS_DIR", str(tmp_path / "runs"))
        store = SQLiteTraceStore(str(path))
        run_ids = _store_runs(store, MAX_PAGE_SIZE + 3)

        app = AppTest.from_file(str(APP), default_timeout=APPTEST_TIMEOUT)
        app.run()
        assert not app.exception
        assert len(app.dataframe[0].value) == MAX_PAGE_SIZE
        assert any(f"newest {MAX_PAGE_SIZE} runs" in item.value for item in app.caption)

        app.sidebar.radio[0].set_value("Replay Proof").run()
        assert not app.exception
        assert len(app.selectbox[0].options) == MAX_PAGE_SIZE
        listed = {option.rsplit(" - ", 1)[1] for option in app.selectbox[0].options}
        # Runs saved within one second tie on SQLite timestamps, so take any run
        # the picker left out rather than assuming which one is oldest.
        unlisted = [run_id for run_id in run_ids if run_id not in listed]
        assert len(unlisted) == len(run_ids) - MAX_PAGE_SIZE
        oldest = unlisted[0]

        _text_input(app, "Baseline trace: older run ID").input(oldest).run()
        assert not app.exception

        app.sidebar.radio[0].set_value("Optimization").run()
        _text_input(app, "Choose trace: older run ID").input(oldest).run()
        assert not app.exception
        assert not app.warning

        _text_input(app, "Choose trace: older run ID").input("missing-run").run()
        assert not app.exception
        assert any("missing-run" in item.value for item in app.warning)
    finally:
        st.cache_resource.clear()
