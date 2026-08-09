"""Invalid operator input must surface inline, not replace the page with a traceback.

Streamlit reruns the whole script on every widget change, so a control that parses or
executes user-controlled input has no natural moment to wait for the input to be
finished. A half-typed repository path or a trace file with one brace missing reached
the library directly and raised, and the operator lost the view they were editing in.
"""

from __future__ import annotations

import json
from pathlib import Path

from streamlit.testing.v1 import AppTest

from agentloop.store import SQLiteTraceStore
from agentloop.tracer import AgentTrace

ROOT = Path(__file__).resolve().parents[1]
APP = str(ROOT / "dashboard" / "app.py")


def _repo_path_input(app):
    """The "Repository path" box, by label.

    Not by index: the sidebar "Project" input is also a text_input, and picking the wrong
    one silently switches project rather than failing, leaving the page with no traces
    and the test asserting against an empty page.
    """
    return next(w for w in app.text_input if w.label == "Repository path")


def _store_with_a_trace(tmp_path, monkeypatch) -> None:
    database = tmp_path / "dashboard.db"
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(database))
    store = SQLiteTraceStore(path=str(database))
    store.save_trace(AgentTrace(name="baseline"))


def test_patch_plan_reports_a_missing_repository_path_inline(tmp_path, monkeypatch) -> None:
    _store_with_a_trace(tmp_path, monkeypatch)

    app = AppTest.from_file(APP, default_timeout=60)
    app.run()
    app.sidebar.radio[0].set_value("Patch Plan").run()
    _repo_path_input(app).set_value(str(tmp_path / "does-not-exist")).run()

    assert not app.exception
    assert any("repository path" in error.value for error in app.error)


def test_patch_plan_reports_a_path_outside_the_allowed_root_inline(tmp_path, monkeypatch) -> None:
    """The other ValueError _resolve_repository_path raises, and the one that matters.

    A traversal attempt is refused rather than crashing, so the refusal is legible.
    """
    _store_with_a_trace(tmp_path, monkeypatch)

    app = AppTest.from_file(APP, default_timeout=60)
    app.run()
    app.sidebar.radio[0].set_value("Patch Plan").run()
    _repo_path_input(app).set_value("../../..").run()

    assert not app.exception
    assert any("repository path" in error.value for error in app.error)


def test_patch_plan_still_works_for_a_valid_path(tmp_path, monkeypatch) -> None:
    """The control: the guard must not swallow the working case."""
    _store_with_a_trace(tmp_path, monkeypatch)

    app = AppTest.from_file(APP, default_timeout=60)
    app.run()
    app.sidebar.radio[0].set_value("Patch Plan").run()

    assert not app.exception
    assert not [error for error in app.error if "repository path" in error.value]


def _upload(app, payload: bytes, name: str = "trace.json"):
    app.file_uploader[0].set_value((name, payload, "application/json"))
    return app.run()


def test_uploaded_trace_with_malformed_json_reports_the_position(tmp_path, monkeypatch) -> None:
    _store_with_a_trace(tmp_path, monkeypatch)

    app = AppTest.from_file(APP, default_timeout=60)
    app.run()
    app.sidebar.radio[0].set_value("Ingest").run()
    _upload(app, b'{"name": "broken"')

    assert not app.exception
    assert any("uploaded trace JSON" in error.value for error in app.error)


def test_uploaded_trace_failing_schema_validation_is_reported(tmp_path, monkeypatch) -> None:
    """Valid JSON, invalid trace -- a different exception from a different layer."""
    _store_with_a_trace(tmp_path, monkeypatch)

    app = AppTest.from_file(APP, default_timeout=60)
    app.run()
    app.sidebar.radio[0].set_value("Ingest").run()
    _upload(app, json.dumps({"not": "a trace"}).encode("utf-8"))

    assert not app.exception
    assert any("uploaded trace JSON" in error.value for error in app.error)


def test_a_valid_uploaded_trace_is_accepted(tmp_path, monkeypatch) -> None:
    """The control: a real trace must still reach the store button."""
    _store_with_a_trace(tmp_path, monkeypatch)
    payload = json.dumps(AgentTrace(name="uploaded").to_dict()).encode("utf-8")

    app = AppTest.from_file(APP, default_timeout=60)
    app.run()
    app.sidebar.radio[0].set_value("Ingest").run()
    _upload(app, payload)

    assert not app.exception
    assert not [error for error in app.error if "uploaded trace JSON" in error.value]
