from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from agentloop.ci import build_ci_report, ci_report_to_markdown
from agentloop.cli import app
from agentloop.tracer import AgentTrace

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "markers,scope",
    [
        ((True, True), "includes_synthetic_data"),
        ((True, False), "includes_synthetic_data"),
        ((False, False), "supplied_trace_comparison"),
        ((None, None), "supplied_trace_comparison"),
    ],
)
def test_ci_reports_trace_provenance(markers, scope):
    traces = [
        AgentTrace(name=side, metadata={"synthetic": marker, "source": "fixture|<script>"})
        for side, marker in zip(("baseline", "candidate"), markers)
    ]
    report = build_ci_report(*traces)
    markdown = ci_report_to_markdown(report)
    assert report["evidence_scope"] == scope
    assert report["trace_inputs"]["baseline"]["run_id"] == traces[0].run_id
    assert "## Trace inputs" in markdown
    assert "<script>" not in markdown
    if True in markers:
        assert "does not establish application performance gains" in markdown
        assert "safe to merge" not in markdown


def test_ci_records_input_paths_in_artifact_and_summary(tmp_path, monkeypatch):
    baseline, candidate = tmp_path / "baseline.json", tmp_path / "candidate.json"
    AgentTrace(name="base", metadata={"synthetic": True}).export_json(baseline)
    AgentTrace(name="candidate", metadata={"synthetic": True}).export_json(candidate)
    output, json_output, summary = (
        tmp_path / "report.md",
        tmp_path / "report.json",
        tmp_path / "summary.md",
    )
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    result = CliRunner().invoke(
        app,
        [
            "ci",
            "--baseline",
            str(baseline),
            "--candidate",
            str(candidate),
            "--out",
            str(output),
            "--json-out",
            str(json_output),
            "--github-step-summary",
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(json_output.read_text(encoding="utf-8"))
    assert report["trace_inputs"]["baseline"]["path"] == str(baseline)
    assert "## Trace inputs" in summary.read_text(encoding="utf-8")


def test_repository_workflow_labels_its_generated_data_as_self_test():
    text = (ROOT / ".github/workflows/agentloop-performance.yml").read_text(encoding="utf-8")
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    assert "self-test" in workflow["name"]
    assert "Performance Proof" not in text
    assert "does not show that this PR improves application performance" in text
    steps = workflow["jobs"]["agentloop-ci"]["steps"]
    generate = next(step for step in steps if step.get("name") == "Generate demo traces")
    assert "AGENTLOOP_GENERATE_DEMO_TRACES == 'true'" in generate["if"]


def test_reusable_workflow_never_generates_missing_application_traces():
    text = (ROOT / ".github/workflows/application-performance.yml").read_text(encoding="utf-8")
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    assert workflow["on"]["workflow_call"]["inputs"]["trace_artifact"]["required"] == "true"
    scripts = "\n".join(step.get("run", "") for step in workflow["jobs"]["compare"]["steps"])
    assert "demo-all" not in scripts and "quickstart" not in scripts
    assert scripts.index('test -f "inputs/$BASELINE"') < scripts.index("agentloop ci")
    assert scripts.index('test -f "inputs/$CANDIDATE"') < scripts.index("agentloop ci")
    assert 'test -f "inputs/$QUALITY"' in scripts
    assert "--no-fail-on-gate" not in scripts
