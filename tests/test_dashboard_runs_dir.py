from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
import streamlit as st
from dashboard_apptest import APPTEST_TIMEOUT
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / "dashboard" / "app.py")


@pytest.fixture(autouse=True)
def _isolate_the_cached_store():
    st.cache_resource.clear()
    yield
    st.cache_resource.clear()


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTLOOP_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(tmp_path / "dashboard.db"))
    monkeypatch.delenv("AGENTLOOP_DASHBOARD_REQUIRE_KEY", raising=False)
    app = AppTest.from_file(APP, default_timeout=APPTEST_TIMEOUT)
    app.run()
    return app


def test_configured_runs_directory_is_used(tmp_path, monkeypatch):
    target = tmp_path / "configured-runs"
    monkeypatch.setenv("AGENTLOOP_DASHBOARD_RUNS_DIR", str(target))
    monkeypatch.chdir(tmp_path)
    app = _app(tmp_path, monkeypatch)
    assert not app.exception
    assert target.is_dir()
    assert not (tmp_path / "runs").exists()


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs POSIX permissions and a non-root user",
)
def test_a_read_only_working_directory_falls_back_to_a_temporary_directory(tmp_path, monkeypatch):
    read_only = tmp_path / "app"
    read_only.mkdir()
    read_only.chmod(stat.S_IRUSR | stat.S_IXUSR)
    monkeypatch.delenv("AGENTLOOP_DASHBOARD_RUNS_DIR", raising=False)
    monkeypatch.chdir(read_only)
    try:
        app = _app(tmp_path, monkeypatch)
        assert not app.exception
    finally:
        read_only.chmod(stat.S_IRWXU)
