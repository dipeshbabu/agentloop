from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentloop.cli import app
from agentloop.tracer import trace_agent, trace_model_call


def _clean(output: str) -> str:
    """Normalize CLI error output for substring matching.

    Typer renders ``BadParameter`` inside a Rich bordered box and wraps the
    message to the terminal width, inserting box-drawing borders and padding
    between words. That can split a phrase (for example ``permission denied``)
    across box lines, so strip the box characters and collapse whitespace before
    asserting on message text.
    """
    return " ".join(re.sub(r"[│╭╮╰╯─]", " ", output).split())


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

    clean = _clean(result.output)
    assert result.exit_code == 2
    assert "Input file does not exist" in clean
    assert "agentloop demo" in clean
    assert not missing.exists()
    assert not output.exists()


def test_trace_command_rejects_directory_input(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["report", str(tmp_path)])

    assert result.exit_code == 2
    assert "Input path is not a regular file" in _clean(result.output)


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

    clean = _clean(result.output)
    assert result.exit_code == 2
    assert "Input file is not readable as UTF-8" in clean
    assert "permission denied" in clean


def test_trace_command_rejects_malformed_json(tmp_path: Path) -> None:
    trace_path = tmp_path / "malformed.json"
    trace_path.write_text('{"name":', encoding="utf-8")

    result = CliRunner().invoke(app, ["report", str(trace_path)])

    assert result.exit_code == 2
    assert "Input file is not valid JSON" in _clean(result.output)


def test_trace_command_rejects_invalid_trace_schema(tmp_path: Path) -> None:
    trace_path = tmp_path / "not-a-trace.json"
    trace_path.write_text('{"events": []}', encoding="utf-8")

    result = CliRunner().invoke(app, ["report", str(trace_path)])

    assert result.exit_code == 2
    assert "Input file is not a valid AgentLoop trace" in _clean(result.output)


def test_unknown_cost_cli_reports_unavailable_instead_of_zero_savings(tmp_path: Path) -> None:
    repeated = "stable context " * 100
    with trace_agent("unknown-cost") as trace:
        for name in ("first", "second"):
            with trace_model_call(
                name,
                model="private-model",
                input_tokens=200,
                output_tokens=10,
                input_text=repeated,
            ):
                pass
    trace_path = tmp_path / "unknown.json"
    trace.export_json(trace_path)

    report = CliRunner().invoke(app, ["report", str(trace_path)])
    optimize = CliRunner().invoke(
        app,
        ["optimize", "--path", str(trace_path), "--out", str(tmp_path / "plan.md")],
    )
    value = CliRunner().invoke(
        app,
        ["value-report", "--path", str(trace_path), "--out", str(tmp_path / "value.json")],
    )

    assert report.exit_code == 0
    assert optimize.exit_code == 0
    assert value.exit_code == 0
    assert "unavailable" in report.output
    assert "unavailable cost" in optimize.output
    assert "unavailable" in value.output
