from __future__ import annotations

from pathlib import Path

import pytest
from workflow_fixtures import pipeline

st = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from agentloop.store import SQLiteTraceStore  # noqa: E402


def test_dashboard_displays_workflow_identity_and_generic_stage_kinds(tmp_path, monkeypatch):
    st.cache_resource.clear()
    try:
        path = tmp_path / "dashboard-workflows.db"
        monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(path))
        store = SQLiteTraceStore(str(path))
        store.save_trace(pipeline(branching=True))
        app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "dashboard" / "app.py"), default_timeout=60
        )
        app.run()
        app.sidebar.radio[0].set_value("Traces").run()
        assert not app.exception
        assert any("mail-router" in item.value for item in app.caption)
        timeline = next(item.value for item in app.dataframe if "Stage" in item.value.columns)
        assert set(timeline["operation_kind"]) == {
            "classifier",
            "rule",
            "retriever",
            "external_service",
        }
        assert set(timeline["Stage"]) == {"classify", "priority", "lookup", "route"}
        assert any(item.label == "Recorded model cost" for item in app.metric)
        app.sidebar.radio[0].set_value("Optimization").run()
        assert not app.exception
        assert any("dependency" in item.value.to_string() for item in app.dataframe)
    finally:
        st.cache_resource.clear()
