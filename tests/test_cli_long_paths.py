"""CLI input errors stay readable when the input path is long.

Rich's error box cannot wrap a long unbroken path and cuts the line with an
ellipsis, so every message must name the problem before the path.
"""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from agentloop.entrypoint import app

_BOX = re.compile(r"[─-╿│|]")


def _clean(text: str) -> str:
    return " ".join(_BOX.sub(" ", text).split())


@pytest.fixture
def deep(tmp_path):
    folder = tmp_path
    for index in range(6):
        folder = folder / f"a-long-directory-name-number-{index}-to-exceed-the-terminal-width"
    folder.mkdir(parents=True)
    return folder


@pytest.mark.parametrize(
    ("content", "command", "reason"),
    [
        ('{"fixtures": []}', "quality-report", "must contain at least one case"),
        ("{not json", "report", "not valid JSON (Expecting property name"),
        ('{"name": 3}', "report", "Input file is not a valid AgentLoop trace ("),
        (b"\xff\xfe\x00bad", "report", "not readable as UTF-8 ("),
    ],
)
def test_reason_is_visible_with_a_long_path(deep, content, command, reason):
    path = deep / "input.json"
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    result = CliRunner().invoke(app, [command, str(path)], terminal_width=80)
    assert result.exit_code == 2
    assert reason in _clean(result.output), result.output


def test_missing_file_names_the_fix_before_the_path(deep):
    result = CliRunner().invoke(app, ["report", str(deep / "missing.json")], terminal_width=80)
    assert result.exit_code == 2
    assert "agentloop demo-all" in _clean(result.output)
