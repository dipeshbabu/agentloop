from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_structured_quality import case
from workflow_fixtures import pipeline

st = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from agentloop.store import SQLiteTraceStore  # noqa: E402


def test_dashboard_renders_indeterminate_structured_scores(tmp_path, monkeypatch):
    st.cache_resource.clear()
    try:
        path = tmp_path / "quality.db"
        monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(path))
        store = SQLiteTraceStore(str(path))
        store.save_trace(pipeline(run_id="before"))
        store.save_trace(pipeline(run_id="after"))
        app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "dashboard" / "app.py"), default_timeout=60
        )
        app.run()
        app.sidebar.radio[0].set_value("Quality Gates").run()
        fixture = case("decision", "x", "x", labels=["x"])
        fixture.pop("candidate_output")
        app.text_area[0].set_value(json.dumps({"schema_version": "2.0", "fixtures": [fixture]}))
        app.button[0].click().run()
        assert not app.exception
        assert (
            next(item.value for item in app.metric if item.label == "Candidate score")
            == "unavailable"
        )
        assert any("Indeterminate cases: 1" in item.value for item in app.caption)
    finally:
        st.cache_resource.clear()
