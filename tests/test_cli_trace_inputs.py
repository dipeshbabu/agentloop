from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentloop.cli import app

TRACE_COMMANDS = [
    pytest.param(["report", "{input}"], id="report"),
    pytest.param(["compare", "--baseline", "{input}", "--optimized", "{input}"], id="compare"),
    pytest.param(
        [
            "replay",
            "--baseline",
            "{input}",
            "--candidate",
            "{input}",
            "--out",
            "{output}",
        ],
        id="replay",
    ),
    pytest.param(
        [
            "ci",
            "--baseline",
            "{input}",
            "--candidate",
            "{input}",
            "--out",
            "{output}",
        ],
        id="ci",
    ),
    pytest.param(["dump-report", "{input}", "{output}"], id="dump-report"),
    pytest.param(["audit", "--path", "{input}", "--out", "{output}"], id="audit"),
    pytest.param(["optimize", "--path", "{input}", "--out", "{output}"], id="optimize"),
    pytest.param(["diagnose", "--path", "{input}", "--out", "{output}"], id="diagnose"),
    pytest.param(["import-otel", "{input}", "{output}"], id="import-otel"),
    pytest.param(["export-otel", "{input}", "{output}"], id="export-otel"),
    pytest.param(["patch", "--path", "{input}", "--out", "{output}"], id="patch"),
    pytest.param(["value-report", "--path", "{input}", "--out", "{output}"], id="value-report"),
    pytest.param(["store-trace", "--path", "{input}"], id="store-trace"),
    pytest.param(["upload", "--path", "{input}"], id="upload"),
]


def _render_args(template: list[str], *, input_path: Path, output_path: Path) -> list[str]:
    return [value.format(input=str(input_path), output=str(output_path)) for value in template]


@pytest.mark.parametrize("template", TRACE_COMMANDS)
def test_trace_commands_reject_missing_inputs_without_side_effects(
    template: list[str], tmp_path: Path
) -> None:
    missing = tmp_path / "missing-trace.json"
    output = tmp_path / "should-not-exist"

    result = CliRunner().invoke(app, _render_args(template, input_path=missing, output_path=output))

    assert result.exit_code == 2
    assert "Input file does not exist" in result.output
    assert "agentloop demo" in result.output
    assert not missing.exists()
    assert not output.exists()


def test_trace_command_rejects_directory_input(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["report", str(tmp_path)])

    assert result.exit_code == 2
    assert "Input path is not a regular file" in result.output


def test_trace_command_rejects_unreadable_input(tmp_path: Path, monkeypatch) -> None:
    trace_path = tmp_path / "unreadable.json"
    trace_path.write_text("{}", encoding="utf-8")
    original_read_text = Path.read_text

    def deny_read(path: Path, *args: object, **kwargs: object) -> str:
        if path == trace_path:
            raise PermissionError("permission denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_read)
    result = CliRunner().invoke(app, ["report", str(trace_path)])

    assert result.exit_code == 2
    assert "Input file is not readable as UTF-8" in result.output
    assert "permission denied" in result.output


def test_trace_command_rejects_malformed_json(tmp_path: Path) -> None:
    trace_path = tmp_path / "malformed.json"
    trace_path.write_text('{"name":', encoding="utf-8")

    result = CliRunner().invoke(app, ["report", str(trace_path)])

    assert result.exit_code == 2
    assert "Input file is not valid JSON" in result.output


def test_trace_command_rejects_invalid_trace_schema(tmp_path: Path) -> None:
    trace_path = tmp_path / "not-a-trace.json"
    trace_path.write_text('{"events": []}', encoding="utf-8")

    result = CliRunner().invoke(app, ["report", str(trace_path)])

    assert result.exit_code == 2
    assert "Input file is not a valid AgentLoop trace" in result.output
