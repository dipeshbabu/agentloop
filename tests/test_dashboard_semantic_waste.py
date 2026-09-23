from __future__ import annotations

from pathlib import Path

import pytest
from test_semantic_findings import prepare

st = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from agentloop.store import SQLiteTraceStore  # noqa: E402


def test_dashboard_queue_and_diagnosis_render_unknown_semantic_savings(tmp_path, monkeypatch):
    st.cache_resource.clear()
    try:
        path = tmp_path / "semantic-dashboard.db"
        monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(path))
        store = SQLiteTraceStore(str(path))
        store.save_trace(prepare())
        app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "dashboard" / "app.py"), default_timeout=60
        )
        app.run()
        app.sidebar.radio[0].set_value("Optimization Queue").run()
        assert not app.exception
        assert (
            next(item.value for item in app.metric if item.label == "Estimated latency savings")
            == "unavailable"
        )
        app.sidebar.radio[0].set_value("Diagnosis").run()
        assert not app.exception
        assert any("unavailable" in item.value for item in app.markdown)
    finally:
        st.cache_resource.clear()
