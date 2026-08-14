"""Invalid operator input must surface inline, not replace the page with a traceback.

Streamlit reruns the whole script on every widget change, so a control that parses or
executes user-controlled input has no natural moment to wait for the input to be
finished. A half-typed repository path or a trace file with one brace missing reached
the library directly and raised, and the operator lost the view they were editing in.

The message wording is pinned in ``test_dashboard_trace_upload.py`` against the helper
directly; these tests pin that the page shows an error rather than raising, that the
store action is gated on a parse that succeeded, and that a stored trace really lands.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from agentloop.store import SQLiteTraceStore
from agentloop.tracer import AgentTrace

ROOT = Path(__file__).resolve().parents[1]
APP = str(ROOT / "dashboard" / "app.py")

STORE_BUTTON = "Store uploaded trace"


@pytest.fixture(autouse=True)
def _isolate_the_cached_store():
    """``app.py`` builds its store under ``@st.cache_resource``, which outlives an AppTest.

    The cache key is the ``load_store`` function, not the database path, so the second
    test in a process gets the first test's store no matter what
    ``AGENTLOOP_SQLITE_PATH`` says -- and the first test's ``tmp_path`` is gone by then.
    Cleared on both sides so neither a leftover from an earlier module nor one left by
    this one can decide a result.
    """

    st.cache_resource.clear()
    yield
    st.cache_resource.clear()


def _repo_path_input(app):
    """The "Repository path" box, by label.

    Not by index: the sidebar "Project" input is also a text_input, and picking the wrong
    one silently switches project rather than failing, leaving the page with no traces
    and the test asserting against an empty page.
    """
    return next(w for w in app.text_input if w.label == "Repository path")


def _store_with_a_trace(tmp_path, monkeypatch) -> SQLiteTraceStore:
    database = tmp_path / "dashboard.db"
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(database))
    store = SQLiteTraceStore(path=str(database))
    store.save_trace(AgentTrace(name="baseline"))
    return store


def _ingest_page(tmp_path, monkeypatch) -> AppTest:
    _store_with_a_trace(tmp_path, monkeypatch)
    app = AppTest.from_file(APP, default_timeout=60)
    app.run()
    app.sidebar.radio[0].set_value("Ingest").run()
    return app


def _upload(app, payload: bytes, name: str = "trace.json"):
    app.file_uploader[0].set_value((name, payload, "application/json"))
    return app.run()


def _store_buttons(app):
    return [button for button in app.button if button.label == STORE_BUTTON]


def _upload_errors(app):
    return [error for error in app.error if "uploaded trace" in error.value]


def test_patch_plan_reports_a_missing_repository_path_inline(tmp_path, monkeypatch) -> None:
    _store_with_a_trace(tmp_path, monkeypatch)

    app = AppTest.from_file(APP, default_timeout=60)
    app.run()
    app.sidebar.radio[0].set_value("Patch Plan").run()
    _repo_path_input(app).set_value(str(tmp_path / "does-not-exist")).run()

    assert not app.exception
    assert any("repository path" in error.value for error in app.error)


def test_patch_plan_reports_a_path_outside_the_allowed_root_inline(tmp_path, monkeypatch) -> None:
    """The other RepositoryPathError, and the one that matters.

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


def test_uploaded_trace_with_malformed_json_reports_the_position(tmp_path, monkeypatch) -> None:
    app = _ingest_page(tmp_path, monkeypatch)
    _upload(app, b'{\n  "name": "broken"\n')

    assert not app.exception
    assert any("line 3, column 1" in error.value for error in _upload_errors(app))


def test_uploaded_trace_that_is_not_utf8_is_reported(tmp_path, monkeypatch) -> None:
    """A file picker accepts any bytes; a JPEG renamed to .json arrives here."""
    app = _ingest_page(tmp_path, monkeypatch)
    _upload(app, b'{"name": "\xff"}')

    assert not app.exception
    assert any("not UTF-8" in error.value for error in _upload_errors(app))


def test_an_empty_uploaded_file_is_reported_inline(tmp_path, monkeypatch) -> None:
    """A 0-byte upload is a boundary the file picker offers on every platform."""
    app = _ingest_page(tmp_path, monkeypatch)
    _upload(app, b"")

    assert not app.exception
    assert any("line 1, column 1" in error.value for error in _upload_errors(app))


def test_uploaded_trace_failing_schema_validation_is_reported(tmp_path, monkeypatch) -> None:
    """Valid JSON, invalid trace -- a different exception from a different layer."""
    app = _ingest_page(tmp_path, monkeypatch)
    _upload(app, json.dumps({"not": "a trace"}).encode("utf-8"))

    assert not app.exception
    assert _upload_errors(app)


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("malformed json", b'{"name": "broken"'),
        ("not utf-8", b'{"name": "\xff"}'),
        ("schema violation", b'{"not": "a trace"}'),
        ("empty file", b""),
    ],
)
def test_an_invalid_upload_hides_the_store_button(label, payload, tmp_path, monkeypatch) -> None:
    """The button must not be reachable when there is no trace behind it.

    Rendering it and failing on click would put the failure a page away from the field
    that caused it, which is what the inline error exists to avoid.
    """
    app = _ingest_page(tmp_path, monkeypatch)
    _upload(app, payload)

    assert not app.exception
    assert not _store_buttons(app), f"{label} left the store button rendered"


def test_a_valid_uploaded_trace_is_accepted(tmp_path, monkeypatch) -> None:
    """The control: a real trace must still reach the store button."""
    app = _ingest_page(tmp_path, monkeypatch)
    _upload(app, json.dumps(AgentTrace(name="uploaded").to_dict()).encode("utf-8"))

    assert not app.exception
    assert not _upload_errors(app)
    assert _store_buttons(app)


def test_storing_a_valid_uploaded_trace_persists_it(tmp_path, monkeypatch) -> None:
    """Past the button: the accepted trace reaches the store under the shown run_id."""
    store = _store_with_a_trace(tmp_path, monkeypatch)
    trace = AgentTrace(name="uploaded")

    app = AppTest.from_file(APP, default_timeout=60)
    app.run()
    app.sidebar.radio[0].set_value("Ingest").run()
    _upload(app, json.dumps(trace.to_dict()).encode("utf-8"))
    _store_buttons(app)[0].click().run()

    assert not app.exception
    assert any(f"`{trace.run_id}`" in success.value for success in app.success)
    assert store.get_trace(run_id=trace.run_id, project_id="default") is not None
