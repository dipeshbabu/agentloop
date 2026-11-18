from __future__ import annotations

import json
import sys

import pytest
from typer.testing import CliRunner

from agentloop.entrypoint import app
from agentloop.experiment_types import fingerprint
from agentloop.improvement import capture, finish, start
from agentloop.improvement_verifier import LocalVerifier
from agentloop.tracer import AgentTrace


@pytest.fixture
def study(tmp_path):
    tasks = tmp_path / "tasks.json"
    tasks.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "name": "Source attribution",
                "tasks": [
                    {
                        "id": "one",
                        "input": {"source": "guide.md"},
                        "expected": {"answer": "yes"},
                        "scorer": {"type": "json_subset"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    runner = tmp_path / "runner.py"
    runner.write_text(
        """import json,sys
from agentloop.tracer import AgentTrace
request = json.load(sys.stdin)
mode = sys.argv[1]
trace = AgentTrace("example", started_at="2026-01-01T00:00:00+00:00",
                   ended_at="2026-01-01T00:00:00.080+00:00", elapsed_ms=80 if mode == "baseline" else 10,
                   metadata={"synthetic": True, "output": {"answer": "yes", "source": "wrong.md" if mode == "wrong" else request["input"]["source"]}})
trace.export_json(request["trace_path"])
""",
        encoding="utf-8",
    )
    verifier = tmp_path / "verify.py"
    verifier.write_text(
        """import json,sys
request = json.load(sys.stdin)
assert set(request) == {"schema_version", "task_id", "repetition", "input", "output"}
assert request["output"]["source"] == request["input"]["source"]
""",
        encoding="utf-8",
    )
    capture(tasks, tmp_path / "baseline", [sys.executable, str(runner), "baseline"])
    return tasks, runner, verifier


@pytest.mark.parametrize("mode,accepted", [("good", True), ("wrong", False)])
def test_cli_verifies_both_sides_and_rejects_a_faster_wrong_source(tmp_path, study, mode, accepted):
    tasks, runner, verifier = study
    cli = CliRunner()
    frozen = cli.invoke(
        app,
        [
            "improve",
            "start",
            "--tasks",
            str(tasks),
            "--baseline",
            str(tmp_path / "baseline/capture.json"),
            "--out",
            str(tmp_path / "session"),
            "--verifier",
            str(verifier),
        ],
    )
    assert frozen.exit_code == 0, frozen.output
    assert "Local verifier passed on 1 of 1 baseline attempts" in frozen.output
    capture(
        tasks,
        tmp_path / "candidate",
        [sys.executable, str(runner), mode],
        control=[sys.executable, str(runner), "baseline"],
    )
    result = cli.invoke(
        app,
        [
            "improve",
            "finish",
            "--session",
            str(tmp_path / "session/session.json"),
            "--candidate",
            str(tmp_path / "candidate/capture.json"),
            "--change",
            "Recorded change",
            "--out",
            str(tmp_path / "results"),
            "--verifier",
            str(verifier),
        ],
    )
    assert result.exit_code == (0 if accepted else 1), result.output
    report = json.loads((tmp_path / "results/report.json").read_bytes())
    assert report["passed"] is accepted
    [case] = report["cases"]
    assert case["verifier"]["baseline"]["passed"] is True
    assert case["verifier"]["candidate"]["passed"] is accepted
    assert case["quality"]["cases"][0]["passed"] is True
    assert case["quality"]["cases"][1]["passed"] is accepted
    assert case["replay"]["deltas"]["latency_improvement_pct"] == 87.5
    assert case["timing"]["eligible"] is accepted
    if not accepted:
        assert "Candidate local verification failed" in result.output
    html = (tmp_path / "results/report.html").read_text(encoding="utf-8")
    assert "Local verifier SHA-256" in html and "Local verifier" in html


def test_saved_session_cannot_authorize_or_change_a_verifier(tmp_path, study):
    tasks, _, verifier = study
    path = tmp_path / "session/session.json"
    start(tasks, tmp_path / "baseline/capture.json", path.parent, verifier=verifier)
    original = verifier.read_bytes()
    with pytest.raises(ValueError, match="explicit --verifier"):
        finish(path, tmp_path / "unused.json", tmp_path / "results", change="Missing selection")
    verifier.write_bytes(original + b"\n# Changed after start\n")
    with pytest.raises(ValueError, match="Verifier source changed"):
        finish(
            path,
            tmp_path / "unused.json",
            tmp_path / "results",
            change="Changed verifier",
            verifier=verifier,
        )
    saved = json.loads(path.read_bytes())
    saved["specification"]["verifier"]["script"] = str(verifier)
    saved["session_hash"] = fingerprint(saved["specification"])
    path.write_text(json.dumps(saved), encoding="utf-8")
    with pytest.raises(ValueError, match="explicit --verifier"):
        finish(path, tmp_path / "unused.json", tmp_path / "results", change="Untrusted path")
    assert not (tmp_path / "results").exists()


def test_verifier_uses_checked_bytes_and_fails_on_timeout(tmp_path):
    verifier = tmp_path / "verify.py"
    verifier.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    snapshot = LocalVerifier.read(verifier, 0.2)
    verifier.write_text("raise SystemExit(0)\n", encoding="utf-8")
    trace = AgentTrace("fixture", metadata={"output": {"value": 1}})
    result = snapshot.check({"id": "one", "input": None}, 0, trace, usable=True)
    assert result["status"] == "timed_out" and result["passed"] is False
    assert "timeout" in result["detail"]


def test_missing_output_never_reaches_verifier(tmp_path):
    marker = tmp_path / "marker"
    verifier = tmp_path / "verify.py"
    verifier.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8"
    )
    trace = AgentTrace("fixture", metadata={"output_unavailable": True, "output": 1})
    result = LocalVerifier.read(verifier, 30).check(
        {"id": "one", "input": None}, 0, trace, usable=True
    )
    assert result["status"] == "unavailable" and result["passed"] is False
    assert not marker.exists()
