"""The docs site and the dashboard use the colours of the AgentLoop logo."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs" / "assets"


def _logo_fills(name: str) -> list[str]:
    return re.findall(r'fill="(#[0-9a-fA-F]{6})"', (ASSETS / name).read_text(encoding="utf-8"))


LIGHT_VIOLET, LIGHT_CORAL, LIGHT_INK = _logo_fills("agentloop-logo-light.svg")
DARK_VIOLET, DARK_CORAL, DARK_INK = _logo_fills("agentloop-logo-dark.svg")


def _css_block(css: str, selector: str) -> str:
    start = css.index(selector)
    return css[start : css.index("}", start)]


def test_docs_accent_text_and_highlight_colours_come_from_the_logo():
    css = (ROOT / "docs" / "stylesheets" / "agentloop.css").read_text(encoding="utf-8")
    light = _css_block(css, '[data-md-color-scheme="default"]')
    dark = _css_block(css, '[data-md-color-scheme="slate"] {')
    for block, violet, coral, ink in (
        (light, LIGHT_VIOLET, LIGHT_CORAL, LIGHT_INK),
        (dark, DARK_VIOLET, DARK_CORAL, DARK_INK),
    ):
        assert f"--md-accent-fg-color: {violet};" in block
        assert f"--md-typeset-a-color: {violet};" in block
        assert f"--al-accent: {violet};" in block
        assert f"--al-coral: {coral};" in block
        assert f"--md-default-fg-color: {ink};" in block


def test_dashboard_theme_colours_come_from_the_logo():
    config = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    assert config["theme"]["light"]["primaryColor"] == LIGHT_VIOLET
    assert config["theme"]["light"]["linkColor"] == LIGHT_VIOLET
    assert config["theme"]["light"]["textColor"] == LIGHT_INK
    assert config["theme"]["dark"]["primaryColor"] == DARK_VIOLET
    assert config["theme"]["dark"]["textColor"] == DARK_INK
    for mode, violet, coral in (
        ("light", LIGHT_VIOLET, LIGHT_CORAL),
        ("dark", DARK_VIOLET, DARK_CORAL),
    ):
        assert config["theme"][mode]["chartCategoricalColors"][:2] == [violet, coral]
    theme = (ROOT / "dashboard" / "theme.py").read_text(encoding="utf-8")
    assert f'"accent": "{LIGHT_VIOLET}"' in theme
    assert f'"accent": "{DARK_VIOLET}"' in theme
