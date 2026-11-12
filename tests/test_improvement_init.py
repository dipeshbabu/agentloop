from __future__ import annotations

import ast
import json
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from agentloop.entrypoint import app
from agentloop.improvement_init import initialize, project_tasks


def run_cli(project, *args, allowed=(0,)):
    result = subprocess.run(
        [sys.executable, "-c", "from agentloop.entrypoint import app; app()", *map(str, args)],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode in allowed, result.stdout + result.stderr
    return result


def test_init_generates_reviewable_python_and_data_only_config(tmp_path):
    root = tmp_path / "project"
    names = initialize(root)
    assert set(names) == {
        "agentloop.toml",
        "agentloop_tasks.json",
        "agentloop_runner.py",
        "agentloop_example.py",
        "AGENTLOOP.md",
    }
    for filename in ("agentloop_runner.py", "agentloop_example.py"):
        ast.parse((root / filename).read_text(encoding="utf-8"))
    assert project_tasks(root / "agentloop.toml") == root / "agentloop_tasks.json"
    assert "expected" not in (root / "agentloop_runner.py").read_text(encoding="utf-8")


def test_init_keeps_existing_application_files_and_refuses_collisions(tmp_path):
    (tmp_path / "app.py").write_text("owned application", encoding="utf-8")
    initialize(tmp_path)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    with pytest.raises(ValueError, match="overwrite"):
        initialize(tmp_path)
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before
    assert (tmp_path / "app.py").read_text() == "owned application"


@pytest.mark.parametrize(
    "target", ["bad", "mod:func()", "mod;run:func", "mod:class", "mod:fn\nprint(1)", ":fn"]
)
def test_init_rejects_code_instead_of_a_python_callable_reference(tmp_path, target):
    with pytest.raises(ValueError):
        initialize(tmp_path / "new", target=target)
    assert not (tmp_path / "new").exists()


def test_generated_adapter_connects_an_async_existing_agent(tmp_path):
    initialize(tmp_path, target="existing:answer")
    (tmp_path / "existing.py").write_text(
        "async def answer(payload):\n    return {'answer': payload['value']}\n", encoding="utf-8"
    )
    worker = tmp_path / "agentloop_runner.py"
    check = subprocess.run(
        [sys.executable, str(worker), "--check"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert check.returncode == 0, check.stderr
    trace = tmp_path / "trace.json"
    result = subprocess.run(
        [sys.executable, str(worker)],
        input=json.dumps({"input": {"value": 3}, "trace_path": str(trace)}),
        text=True,
        capture_output=True,
        cwd=tmp_path,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(trace.read_text())["metadata"]["output"] == {"answer": 3}


def test_generated_adapter_retains_failure_and_actionable_diagnostic(tmp_path):
    initialize(tmp_path, target="existing:answer")
    (tmp_path / "existing.py").write_text(
        "def answer(payload):\n    raise ValueError('PRIVATE_CANARY')\n", encoding="utf-8"
    )
    trace = tmp_path / "failed.json"
    result = subprocess.run(
        [sys.executable, "agentloop_runner.py"],
        input=json.dumps({"input": {}, "trace_path": str(trace)}),
        text=True,
        capture_output=True,
        cwd=tmp_path,
        timeout=30,
    )
    assert result.returncode == 1
    assert "ValueError" in result.stderr and "--debug" in result.stderr
    assert "PRIVATE_CANARY" not in result.stderr
    assert json.loads(trace.read_text())["metadata"]["output_unavailable"] is True


def test_complete_offline_onboarding_with_manual_edit_and_rejected_candidate(tmp_path):
    initialize(tmp_path)
    run_cli(
        tmp_path,
        "improve",
        "capture",
        "--out",
        "baseline",
        "--",
        sys.executable,
        "agentloop_runner.py",
    )
    run_cli(tmp_path, "improve", "start", "--baseline", "baseline/capture.json", "--out", "session")
    agent = tmp_path / "agentloop_example.py"
    code = agent.read_text(encoding="utf-8")
    agent.write_text(
        code.replace(
            'return {"notes": [lookup_note(key) for _ in range(3)]}',
            'value = lookup_note(key)\n    return {"notes": [value] * 3}',
        ),
        encoding="utf-8",
    )
    run_cli(
        tmp_path,
        "improve",
        "capture",
        "--out",
        "candidate",
        "--",
        sys.executable,
        "agentloop_runner.py",
    )
    run_cli(
        tmp_path,
        "improve",
        "finish",
        "--session",
        "session/session.json",
        "--candidate",
        "candidate/capture.json",
        "--change",
        "Reuse a request-local lookup",
        "--out",
        "results",
        allowed=(0, 1),
    )
    report = json.loads((tmp_path / "results/report.json").read_text())
    assert report["planned_attempts"] == 2 and report["synthetic"] is True
    assert all(case["quality"]["passed"] for case in report["cases"])
    assert all(
        case["replay"]["candidate"]["operation_counts"]["tool"] == 1 for case in report["cases"]
    )
    # Wall-clock acceptance remains an observed result; this test checks the fixed correctness/call contracts.
    agent.write_text(
        agent.read_text().replace(
            'return {"notes": [value] * 3}', 'return {"notes": ["wrong"] * 3}'
        ),
        encoding="utf-8",
    )
    run_cli(
        tmp_path,
        "improve",
        "capture",
        "--out",
        "wrong",
        "--",
        sys.executable,
        "agentloop_runner.py",
    )
    run_cli(
        tmp_path,
        "improve",
        "finish",
        "--session",
        "session/session.json",
        "--candidate",
        "wrong/capture.json",
        "--change",
        "Incorrect result",
        "--out",
        "rejected",
        allowed=(1,),
    )
    assert json.loads((tmp_path / "rejected/report.json").read_text())["passed_attempts"] == 0


def test_missing_project_config_has_an_actionable_cli_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        app, ["improve", "start", "--baseline", "missing.json", "--out", "session"]
    )
    assert result.exit_code == 2
    assert "--tasks" in result.output and "improve init" in result.output


def test_project_config_cannot_supply_an_executable_runner(tmp_path):
    config = tmp_path / "agentloop.toml"
    config.write_text(
        'schema_version="1.0"\ntasks="tasks.json"\ncommand="untrusted"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="only schema_version and tasks"):
        project_tasks(config)


@pytest.mark.parametrize("failure", ["float('nan')", "object()", "KeyboardInterrupt()"])
def test_adapter_preserves_invalid_output_and_cancelled_attempts(tmp_path, failure):
    initialize(tmp_path, target="existing:answer")
    statement = "raise " + failure if failure == "KeyboardInterrupt()" else "return " + failure
    (tmp_path / "existing.py").write_text(
        "def answer(payload):\n    " + statement + "\n", encoding="utf-8"
    )
    result = run_cli(
        tmp_path,
        "improve",
        "capture",
        "--out",
        "failed",
        "--",
        sys.executable,
        "agentloop_runner.py",
        allowed=(1,),
    )
    manifest = json.loads((tmp_path / "failed/capture.json").read_text())
    status = "cancelled" if failure == "KeyboardInterrupt()" else "failed"
    assert all(row["status"] == status and row["trace"] for row in manifest["runs"])
    assert "--show-runner-output" in result.stdout or "interrupted" in result.stdout


def test_init_rolls_back_its_files_after_a_partial_write(tmp_path, monkeypatch):
    from pathlib import Path

    original = Path.open

    class BrokenWrite:
        def __init__(self, file):
            self.file = file

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.file.close()

        def write(self, value):
            self.file.write(value[:8])
            raise OSError("disk full")

    def opened(path, *args, **kwargs):
        file = original(path, *args, **kwargs)
        return BrokenWrite(file) if path.name == "agentloop_runner.py" and args == ("x",) else file

    monkeypatch.setattr(Path, "open", opened)
    (tmp_path / "owned.txt").write_text("application data")
    with pytest.raises(OSError, match="disk full"):
        initialize(tmp_path)
    assert [path.name for path in tmp_path.iterdir()] == ["owned.txt"]
