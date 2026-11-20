"""A shared dashboard signs users in with a project API key and shows only that project."""

from __future__ import annotations

from pathlib import Path

import pytest

st = pytest.importorskip("streamlit")
from dashboard_apptest import APPTEST_TIMEOUT  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from agentloop.store import SQLiteTraceStore  # noqa: E402
from agentloop.tracer import AgentTrace  # noqa: E402
from dashboard.access import (  # noqa: E402
    LOCKOUT_SECONDS,
    MAX_FAILURES,
    RECHECK_SECONDS,
    lockout_remaining,
    require_project_key,
    session_valid,
    sign_in,
    sign_out,
    visible_pages,
)

APP = str(Path(__file__).resolve().parents[1] / "dashboard" / "app.py")
PAGES = ["Overview", "Patch Plan", "API Keys", "Ingest"]


@pytest.fixture(autouse=True)
def _isolate_the_cached_store():
    st.cache_resource.clear()
    yield
    st.cache_resource.clear()


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", True), ("1", True), (" YES ", True), ("on", True), ("false", False), ("", False)],
)
def test_project_key_mode_is_opt_in(monkeypatch, value, expected):
    monkeypatch.setenv("AGENTLOOP_DASHBOARD_REQUIRE_KEY", value)
    assert require_project_key() is expected


def test_project_users_do_not_see_admin_or_host_file_pages():
    assert visible_pages(PAGES, project_mode=False) == PAGES
    assert visible_pages(PAGES, project_mode=True) == ["Overview", "Ingest"]


def _store(tmp_path):
    store = SQLiteTraceStore(path=str(tmp_path / "dashboard.db"))
    store.save_trace(AgentTrace(name="alpha-agent"), project_id="alpha")
    store.save_trace(AgentTrace(name="beta-agent"), project_id="beta")
    return store


def test_sign_in_binds_the_project_of_a_valid_key(tmp_path):
    store = _store(tmp_path)
    key = store.create_api_key(project_id="alpha", name="team")["api_key"]
    state = {}
    assert sign_in(store, f"  {key}\n", state) == "alpha"
    assert state["signed_in_project"] == "alpha"
    assert key.startswith(state["signed_in_key_prefix"])
    sign_out(state)
    assert "signed_in_project" not in state


@pytest.mark.parametrize("bad", ["", "   ", "al_not-a-real-key", None])
def test_invalid_keys_are_refused(tmp_path, bad):
    state = {}
    assert sign_in(_store(tmp_path), bad, state) is None
    assert "signed_in_project" not in state


def test_repeated_failures_lock_sign_in_for_a_while(tmp_path):
    store = _store(tmp_path)
    key = store.create_api_key(project_id="alpha", name="team")["api_key"]
    state = {}
    for _ in range(MAX_FAILURES):
        assert sign_in(store, "al_wrong", state, now=100.0) is None
    assert lockout_remaining(state, now=100.0) == pytest.approx(LOCKOUT_SECONDS)
    # Even the right key waits out the lockout.
    assert sign_in(store, key, state, now=100.0 + LOCKOUT_SECONDS - 1) is None
    assert sign_in(store, key, state, now=100.0 + LOCKOUT_SECONDS + 1) == "alpha"


def _app(tmp_path, monkeypatch, *, require_key):
    database = tmp_path / "dashboard.db"
    monkeypatch.setenv("AGENTLOOP_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(database))
    if require_key:
        monkeypatch.setenv("AGENTLOOP_DASHBOARD_REQUIRE_KEY", "true")
    else:
        monkeypatch.delenv("AGENTLOOP_DASHBOARD_REQUIRE_KEY", raising=False)
    store = _store(tmp_path)
    key = store.create_api_key(project_id="alpha", name="team")["api_key"]
    app = AppTest.from_file(APP, default_timeout=APPTEST_TIMEOUT)
    app.run()
    return app, key


def _sign_in(app, key):
    field = next(w for w in app.text_input if w.label == "Project API key")
    field.input(key)
    next(b for b in app.button if b.label == "Sign in").click()
    return app.run()


def _shown_names(app):
    return {
        str(value)
        for frame in app.dataframe
        for column in frame.value.columns
        if column == "name"
        for value in frame.value[column]
    }


def test_hosted_dashboard_shows_only_the_signed_in_project(tmp_path, monkeypatch):
    app, key = _app(tmp_path, monkeypatch, require_key=True)
    assert not app.exception
    assert [w.label for w in app.text_input] == ["Project API key"]
    assert not app.sidebar.radio, "no page is reachable before signing in"

    _sign_in(app, "al_wrong-key")
    assert any("not valid" in error.value for error in app.error)
    assert not app.sidebar.radio

    _sign_in(app, key)
    assert not app.exception
    assert any("`alpha`" in md.value for md in app.sidebar.markdown)
    assert "Project" not in [w.label for w in app.sidebar.text_input]
    options = app.sidebar.radio[0].options
    assert "API Keys" not in options and "Patch Plan" not in options
    names = _shown_names(app)
    assert "alpha-agent" in names
    assert "beta-agent" not in names

    next(b for b in app.sidebar.button if b.label == "Sign out").click()
    app.run()
    assert [w.label for w in app.text_input] == ["Project API key"]


def test_local_dashboard_keeps_the_free_project_field(tmp_path, monkeypatch):
    app, _ = _app(tmp_path, monkeypatch, require_key=False)
    assert not app.exception
    assert [w.label for w in app.sidebar.text_input] == ["Project"]
    assert "API Keys" in app.sidebar.radio[0].options


def test_open_sessions_lose_access_after_their_key_is_revoked(tmp_path):
    store = _store(tmp_path)
    created = store.create_api_key(project_id="alpha", name="team")
    state = {}
    assert sign_in(store, created["api_key"], state, now=0.0) == "alpha"
    assert session_valid(store, state, now=RECHECK_SECONDS - 1)
    store.revoke_api_key("alpha", created["prefix"])
    # Within the recheck window the session continues; after it, it ends.
    assert session_valid(store, state, now=RECHECK_SECONDS - 1)
    assert not session_valid(store, state, now=RECHECK_SECONDS + 1)
    assert "signed_in_project" not in state and "signed_in_key" not in state


def test_a_valid_session_is_rechecked_and_kept(tmp_path):
    store = _store(tmp_path)
    key = store.create_api_key(project_id="alpha", name="team")["api_key"]
    state = {}
    sign_in(store, key, state, now=0.0)
    assert session_valid(store, state, now=RECHECK_SECONDS + 1)
    assert state["signed_in_checked_at"] == RECHECK_SECONDS + 1
    assert not session_valid(store, {}, now=0.0)


def test_shared_ingest_page_offers_only_uploads(tmp_path, monkeypatch):
    app, key = _app(tmp_path, monkeypatch, require_key=True)
    _sign_in(app, key)
    app.sidebar.radio[0].set_value("Ingest").run()
    assert not app.exception
    labels = [button.label for button in app.button]
    assert not any(label.startswith("Generate") for label in labels)
    assert app.file_uploader, "uploading into the signed-in project stays available"


def test_traces_page_deletes_a_trace_after_confirmation(tmp_path, monkeypatch):
    app, key = _app(tmp_path, monkeypatch, require_key=True)
    _sign_in(app, key)
    app.sidebar.radio[0].set_value("Traces").run()
    assert not app.exception
    delete = next(b for b in app.button if b.label == "Delete trace")
    assert delete.disabled, "deleting needs the confirmation checkbox"
    next(c for c in app.checkbox if c.label.startswith("I want to delete")).check().run()
    next(b for b in app.button if b.label == "Delete trace").click().run()
    assert not app.exception
    assert any("Deleted" in success.value for success in app.success)
    store = SQLiteTraceStore(path=str(tmp_path / "dashboard.db"))
    assert store.list_traces(project_id="alpha") == []
    assert len(store.list_traces(project_id="beta")) == 1
