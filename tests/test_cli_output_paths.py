from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentloop.cli_paths import validate_output_paths
from agentloop.entrypoint import _quickstart_trace, app
from agentloop.otel import trace_to_otel


@pytest.fixture
def evidence(tmp_path):
    trace = _quickstart_trace()
    trace.metadata["output"] = "ok"
    paths = {name: tmp_path / f"{name}.json" for name in ("baseline", "candidate", "fixtures")}
    trace.export_json(paths["baseline"])
    trace.run_id = "run_candidate"
    for event in trace.events:
        event.run_id = trace.run_id
    trace.export_json(paths["candidate"])
    paths["fixtures"].write_text(json.dumps({"fixtures": [{"expected": "ok"}]}), encoding="utf-8")
    return paths


SOURCE_OVERWRITES = [
    ["dump-report", "{candidate}", "{candidate}"],
    ["audit", "--path", "{candidate}", "--out", "{candidate}"],
    ["optimize", "--path", "{candidate}", "--out", "{candidate}"],
    ["optimize", "--path", "{candidate}", "--out", "{report}", "--json-out", "{candidate}"],
    ["diagnose", "--path", "{candidate}", "--out", "{candidate}"],
    ["patch", "--path", "{candidate}", "--out", "{candidate}"],
    ["value-report", "--path", "{candidate}", "--out", "{candidate}"],
    ["export-otel", "{candidate}", "{candidate}"],
    ["import-otel", "{otel}", "{otel}"],
    ["onboard", "{candidate}", "--out", "{candidate}"],
    ["quality-report", "{fixtures}", "--candidate", "{candidate}", "--out", "{fixtures}"],
    [
        "quality-report",
        "{fixtures}",
        "--candidate",
        "{candidate}",
        "--out",
        "{report}",
        "--json-out",
        "{candidate}",
    ],
    ["replay", "--baseline", "{baseline}", "--candidate", "{candidate}", "--out", "{baseline}"],
    ["ci", "--baseline", "{baseline}", "--candidate", "{candidate}", "--out", "{candidate}"],
    ["analyze", "{candidate}", "--html", "{candidate}"],
    ["analyze", "{candidate}", "--baseline", "{baseline}", "--html", "{baseline}"],
    [
        "analyze",
        "{candidate}",
        "--baseline",
        "{baseline}",
        "--quality-fixtures",
        "{fixtures}",
        "--html",
        "{fixtures}",
    ],
]


@pytest.mark.parametrize("template", SOURCE_OVERWRITES)
def test_report_commands_preserve_all_source_files(template, evidence, tmp_path):
    evidence["otel"] = tmp_path / "otel.json"
    evidence["otel"].write_text(json.dumps(trace_to_otel(_quickstart_trace())), encoding="utf-8")
    report = tmp_path / "new-folder" / "report.md"
    originals = {path: path.read_bytes() for path in evidence.values()}
    fields = {**evidence, "report": report}
    args = [value.format(**fields) for value in template]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2, result.output
    assert {path: path.read_bytes() for path in originals} == originals
    assert not report.parent.exists()


@pytest.mark.parametrize(
    "command", ["replay", "ci", "optimize", "diagnose", "patch", "quality-report"]
)
def test_paired_reports_reject_one_destination(command, evidence, tmp_path):
    output = tmp_path / "reports" / "shared"
    if command in {"replay", "ci"}:
        args = [
            command,
            "--baseline",
            str(evidence["baseline"]),
            "--candidate",
            str(evidence["candidate"]),
        ]
    elif command == "quality-report":
        args = [command, str(evidence["fixtures"]), "--candidate", str(evidence["candidate"])]
    else:
        args = [command, "--path", str(evidence["candidate"])]
    result = CliRunner().invoke(app, [*args, "--out", str(output), "--json-out", str(output)])
    assert result.exit_code == 2, result.output
    assert not output.parent.exists()


@pytest.mark.parametrize("link_type", ["symlink", "hardlink"])
def test_analyze_rejects_link_aliases_without_writing_any_reports(link_type, evidence, tmp_path):
    source = evidence["candidate"]
    alias = tmp_path / "alias.json"
    try:
        if link_type == "symlink":
            alias.symlink_to(source)
        else:
            alias.hardlink_to(source)
    except OSError:
        pytest.skip(f"filesystem does not permit {link_type}")
    original = source.read_bytes()
    report = tmp_path / "reports" / "new.html"
    result = CliRunner().invoke(
        app, ["analyze", str(source), "--json-out", str(alias), "--html", str(report)]
    )
    assert result.exit_code == 2, result.output
    assert source.read_bytes() == alias.read_bytes() == original
    assert not report.parent.exists()


def test_outputs_can_be_regenerated_when_sources_are_distinct(evidence, tmp_path):
    output = tmp_path / "report.json"
    output.write_text("stale output", encoding="utf-8")
    original = evidence["candidate"].read_bytes()
    args = ["analyze", str(evidence["candidate"]), "--json-out", str(output)]
    for _ in range(2):
        result = CliRunner().invoke(app, args)
        assert result.exit_code == 0, result.output
        assert json.loads(output.read_text(encoding="utf-8"))["trace"]["run_id"] == "run_candidate"
    assert evidence["candidate"].read_bytes() == original


def test_ci_step_summary_cannot_append_to_source(evidence, tmp_path, monkeypatch):
    source = evidence["candidate"]
    original = source.read_bytes()
    report = tmp_path / "reports" / "ci.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(source))
    result = CliRunner().invoke(
        app,
        [
            "ci",
            "--baseline",
            str(evidence["baseline"]),
            "--candidate",
            str(source),
            "--out",
            str(report),
            "--github-step-summary",
        ],
    )
    assert result.exit_code == 2, result.output
    assert source.read_bytes() == original
    assert not report.parent.exists()


def test_path_check_errors_fail_closed(tmp_path, monkeypatch):
    def deny_resolve(*args, **kwargs):
        raise PermissionError("not readable")

    monkeypatch.setattr(Path, "resolve", deny_resolve)
    import typer

    with pytest.raises(typer.BadParameter, match="Cannot verify output paths"):
        validate_output_paths(sources=[tmp_path / "input"], outputs={"--out": tmp_path / "output"})


@pytest.mark.parametrize("destination", ["manifest", "trace", "same_reports"])
def test_study_summaries_preserve_manifest_and_traces(destination, evidence, tmp_path):
    manifest = tmp_path / "study.json"
    payload = {
        "schema_version": "1.0",
        "name": "study",
        "baseline": "baseline",
        "conditions": {"baseline": ["baseline.json"], "candidate": ["candidate.json"]},
        "pairing_keys": ["synthetic"],
    }
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    originals = {path: path.read_bytes() for path in [manifest, *evidence.values()]}
    report = tmp_path / "reports" / "study.md"
    json_out = {"manifest": manifest, "trace": evidence["baseline"], "same_reports": report}[
        destination
    ]
    result = CliRunner().invoke(
        app,
        ["study", "summarize", str(manifest), "--out", str(report), "--json-out", str(json_out)],
    )
    assert result.exit_code == 2, result.output
    assert {path: path.read_bytes() for path in originals} == originals
    assert not report.parent.exists()


def test_analyze_cannot_replace_its_input(evidence):
    path = evidence["candidate"]
    original = path.read_bytes()
    result = CliRunner().invoke(app, ["analyze", str(path), "--json-out", str(path)])
    assert result.exit_code == 2, result.output
    assert path.read_bytes() == original


def test_analyze_outputs_must_be_distinct(evidence, tmp_path):
    output = tmp_path / "report"
    result = CliRunner().invoke(
        app,
        ["analyze", str(evidence["candidate"]), "--json-out", str(output), "--html", str(output)],
    )
    assert result.exit_code == 2, result.output
    assert not output.exists()


def test_analyze_rejects_nested_output_files_before_creating_either(evidence, tmp_path):
    json_out = tmp_path / "report"
    html_out = json_out / "index.html"
    result = CliRunner().invoke(
        app,
        [
            "analyze",
            str(evidence["candidate"]),
            "--json-out",
            str(json_out),
            "--html",
            str(html_out),
        ],
    )
    assert result.exit_code == 2, result.output
    assert not json_out.exists()


def test_analyze_rejects_directory_output_before_writing_json(evidence, tmp_path):
    json_out = tmp_path / "report.json"
    result = CliRunner().invoke(
        app,
        [
            "analyze",
            str(evidence["candidate"]),
            "--json-out",
            str(json_out),
            "--html",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 2, result.output
    assert not json_out.exists()


def test_analyze_rejects_file_parent_before_writing_json(evidence, tmp_path):
    json_out = tmp_path / "report.json"
    file_parent = tmp_path / "file-parent"
    file_parent.write_text("keep me", encoding="utf-8")
    result = CliRunner().invoke(
        app,
        [
            "analyze",
            str(evidence["candidate"]),
            "--json-out",
            str(json_out),
            "--html",
            str(file_parent / "report.html"),
        ],
    )
    assert result.exit_code == 2, result.output
    assert not json_out.exists()
    assert file_parent.read_text(encoding="utf-8") == "keep me"


def test_quickstart_outputs_must_be_distinct(tmp_path):
    output = tmp_path / "trace.json"
    result = CliRunner().invoke(
        app, ["quickstart", "--out", str(output), "--json-out", str(output)]
    )
    assert result.exit_code == 2, result.output
    assert not output.exists()


@pytest.mark.parametrize("command", ["replay", "ci"])
def test_comparison_reports_cannot_replace_quality_fixtures(command, evidence, tmp_path):
    originals = {path: path.read_bytes() for path in evidence.values()}
    report = tmp_path / "report.md"
    result = CliRunner().invoke(
        app,
        [
            command,
            "--baseline",
            str(evidence["baseline"]),
            "--candidate",
            str(evidence["candidate"]),
            "--quality-fixtures",
            str(evidence["fixtures"]),
            "--out",
            str(report),
            "--json-out",
            str(evidence["fixtures"]),
        ],
    )
    assert result.exit_code == 2, result.output
    assert not report.exists()
    assert {path: path.read_bytes() for path in originals} == originals
