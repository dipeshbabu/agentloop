"""Contract for every dashboard page's structure while app.py is split (#407).

Each page is rendered through Streamlit's AppTest on the same seeded store, and
its element tree is reduced to a stable outline: element types, plus labels and
headings with run-specific values (IDs, numbers, timestamps) masked. The
outline in tests/fixtures/dashboard_surface.json was recorded from main before
the split. Regenerate deliberately, after reviewing a real UI change, with
AGENTLOOP_UPDATE_GOLDEN=1.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

st = pytest.importorskip("streamlit")
from dashboard_apptest import APPTEST_TIMEOUT  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from agentloop.demo import run_baseline, run_optimized  # noqa: E402
from agentloop.store import SQLiteTraceStore  # noqa: E402
from agentloop.tracer import AgentTrace  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "dashboard" / "app.py"
SNAPSHOT = Path(__file__).parent / "fixtures" / "dashboard_surface.json"
LABELLED = {"header", "subheader", "title", "metric", "button", "selectbox", "radio",
            "text_input", "number_input", "checkbox", "toggle", "tab", "expander",
            "download_button", "file_uploader", "multiselect", "slider", "text_area"}  # fmt: skip
_VOLATILE = re.compile(r"(run_[0-9a-f]+|evt_[0-9a-f]+|[0-9a-f]{12,}|\d[\d,.:%$-]*)")


def _mask(text: object) -> str:
    return _VOLATILE.sub("#", str(text or ""))[:120]


def _outline(node, depth=0, out=None):
    out = [] if out is None else out
    kind = getattr(node, "type", type(node).__name__)
    entry = kind
    if kind in LABELLED:
        label = getattr(node, "label", None) or getattr(node, "value", None)
        entry += ": " + _mask(label)
    out.append("  " * depth + entry)
    children = getattr(node, "children", None) or {}
    for child in children.values() if isinstance(children, dict) else children:
        _outline(child, depth + 1, out)
    return out


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    st.cache_resource.clear()
    st.cache_data.clear()
    path = tmp_path / "surface.db"
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(path))
    monkeypatch.setenv("AGENTLOOP_DASHBOARD_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.delenv("AGENTLOOP_DASHBOARD_REQUIRE_API_KEY", raising=False)
    store = SQLiteTraceStore(str(path))
    demo = tmp_path / "demo"
    store.save_trace(AgentTrace.from_json(run_baseline(demo)))
    store.save_trace(AgentTrace.from_json(run_optimized(demo)))
    yield
    st.cache_resource.clear()
    st.cache_data.clear()


def _pages(app: AppTest) -> list[str]:
    return list(app.sidebar.radio[0].options)


def render_all() -> dict[str, list[str]]:
    app = AppTest.from_file(str(APP), default_timeout=APPTEST_TIMEOUT)
    app.run()
    assert not app.exception
    outlines = {}
    for option in _pages(app):
        app.sidebar.radio[0].set_value(option).run()
        assert not app.exception, option
        outlines[option] = _outline(app.main)
    return outlines


def test_every_page_renders_with_the_same_structure(seeded):
    current = render_all()
    if os.environ.get("AGENTLOOP_UPDATE_GOLDEN") == "1":
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert list(current) == list(expected)
    for page in expected:
        assert current[page] == expected[page], page
