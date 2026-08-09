from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_codeql_action_updates_are_grouped() -> None:
    config = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")

    assert "groups:" in config
    assert "codeql-action:" in config
    assert '          - "github/codeql-action/*"' in config
