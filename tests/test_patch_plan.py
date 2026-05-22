from __future__ import annotations

import json

from typer.testing import CliRunner

from agentloop.cli import app
from agentloop.patches import build_patch_plan, patch_plan_to_markdown
from agentloop.tracer import trace_agent, trace_model_call, trace_retry, trace_tool_call


def test_build_patch_plan_targets_likely_python_files(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "agent_workflow.py").write_text(
        """
async def read_source(url):
    return url

def plan():
    return "done"
""".strip(),
        encoding="utf-8",
    )

    with trace_agent("patchable") as trace:
        for _ in range(3):
            with trace_tool_call("read_source"):
                pass
        repeated = "same stable instruction " * 100
        with trace_model_call("plan", input_text=repeated, output_tokens=1):
            pass
        with trace_model_call("plan", input_text=repeated, output_tokens=1):
            pass
        with trace_retry("repair_json"):
            pass

    plan = build_patch_plan(trace, repo_path=repo)

    assert plan["dry_run"] is True
    assert plan["summary"]["patch_count"] >= 3
    assert "plain_python" in plan["summary"]["frameworks_detected"]
    parallel = next(item for item in plan["patch_plans"] if item["type"] == "parallelize_tools")
    assert parallel["files"][0]["path"] == "agent_workflow.py"
    assert parallel["files"][0]["symbols"] == ["read_source"]
    assert parallel["files"][0]["locations"][0]["line"] == 1
    assert parallel["evidence_spans"]
    assert "asyncio.gather" in parallel["proposed_rewrite"]
    assert "Before:" in parallel["suggested_diff"]
    assert "Dry-run only" in parallel["notes"][0]


def test_patch_plan_markdown_includes_validation(tmp_path) -> None:
    with trace_agent("patchable") as trace:
        for _ in range(3):
            with trace_tool_call("read_source"):
                pass

    plan = build_patch_plan(trace, repo_path=tmp_path)
    markdown = patch_plan_to_markdown(plan)

    assert "# AgentLoop Patch Plan" in markdown
    assert "Validation command: `agentloop replay`" in markdown
    assert "Suggested diff shape" in markdown
    assert "Patch Plans" in markdown


def test_cli_patch_dry_run_writes_markdown_and_json(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "workflow.py").write_text("def read_source(url):\n    return url\n", encoding="utf-8")

    with trace_agent("cli_patch") as trace:
        for _ in range(3):
            with trace_tool_call("read_source"):
                pass
    trace_path = tmp_path / "trace.json"
    trace.export_json(trace_path)

    out = tmp_path / "patch_plan.md"
    json_out = tmp_path / "patch_plan.json"
    result = CliRunner().invoke(
        app,
        [
            "patch",
            "--path",
            str(trace_path),
            "--repo",
            str(repo),
            "--out",
            str(out),
            "--json-out",
            str(json_out),
        ],
    )

    assert result.exit_code == 0
    assert out.exists()
    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert payload["summary"]["patch_count"] >= 1
    assert payload["patch_plans"][0]["files"][0]["path"] == "workflow.py"
