"""The dashboard's navigation, page headers and theme assets."""

from __future__ import annotations

import html
import tomllib
from pathlib import Path

import pytest

st = pytest.importorskip("streamlit")
from dashboard_apptest import APPTEST_TIMEOUT  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from dashboard.theme import ASSETS, NAV_SEPARATOR, PAGE_NAMES, PAGES, nav_label  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def test_every_page_has_an_icon_and_a_description():
    assert len(set(PAGE_NAMES)) == len(PAGE_NAMES)
    for page in PAGES:
        assert page.icon.startswith(":material/") and page.icon.endswith(":")
        assert page.description.endswith(".")
        assert nav_label(page.name) == f"{page.icon}{NAV_SEPARATOR}{page.name}"
    assert nav_label("Unknown") == "Unknown"


def test_theme_defines_light_and_dark_palettes_and_ships_its_logo():
    config = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    for mode in ("light", "dark"):
        palette = config["theme"][mode]
        assert {"primaryColor", "backgroundColor", "textColor"} <= set(palette)
        assert "backgroundColor" in palette["sidebar"]
    # The dashboard ships exact copies of the project logo in docs/assets.
    for name in ("agentloop-logo-light.svg", "agentloop-logo-dark.svg", "agentloop-mark.svg"):
        shipped = (ASSETS / name).read_bytes()
        assert shipped == (ROOT / "docs" / "assets" / name).read_bytes(), name
    assert sorted(path.name for path in ASSETS.iterdir()) == [
        "agentloop-logo-dark.svg",
        "agentloop-logo-light.svg",
        "agentloop-mark.svg",
    ]
    assert "COPY .streamlit ./.streamlit" in (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_each_page_renders_its_header(tmp_path, monkeypatch):
    st.cache_resource.clear()
    try:
        monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(tmp_path / "theme.db"))
        monkeypatch.setenv("AGENTLOOP_DASHBOARD_RUNS_DIR", str(tmp_path / "runs"))
        monkeypatch.delenv("AGENTLOOP_DASHBOARD_REQUIRE_KEY", raising=False)
        app = AppTest.from_file(str(ROOT / "dashboard" / "app.py"), default_timeout=APPTEST_TIMEOUT)
        app.run()
        assert any("<style>" in item.value for item in app.markdown)
        for page in PAGES:
            app.sidebar.radio[0].set_value(page.name).run()
            assert not app.exception, page.name
            headers = [item.value for item in app.markdown if 'class="al-header"' in item.value]
            assert len(headers) == 1
            assert f"<h1>{html.escape(page.name)}</h1>" in headers[0]
            assert html.escape(page.description) in headers[0]
    finally:
        st.cache_resource.clear()
