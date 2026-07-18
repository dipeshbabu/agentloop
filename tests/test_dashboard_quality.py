from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

from agentloop.store import SQLiteTraceStore
from agentloop.tracer import AgentTrace

ROOT = Path(__file__).resolve().parents[1]


def test_quality_gate_dashboard_reports_empty_fixture_suite_inline(tmp_path, monkeypatch) -> None:
    database = tmp_path / "dashboard.db"
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(database))
    store = SQLiteTraceStore(path=str(database))
    store.save_trace(AgentTrace(name="baseline"))
    store.save_trace(AgentTrace(name="candidate"))

    app = AppTest.from_file(str(ROOT / "dashboard" / "app.py"), default_timeout=60)
    app.run()
    app.sidebar.radio[0].set_value("Quality Gates").run()
    app.text_area[0].set_value('{"fixtures": []}')
    app.button[0].click().run()

    assert not app.exception
    assert any("at least one case" in error.value for error in app.error)
