from __future__ import annotations

import json
import sys
from types import ModuleType

import pytest
from typer.testing import CliRunner

from agentloop.entrypoint import app
from agentloop.experiment_types import fingerprint
from agentloop.improvement import capture, finish, read_workload, start

WORKER = """import json,sys,time
from pathlib import Path
from agentloop.tracer import AgentTrace
from agentloop.events import AgentEvent
request=json.loads(sys.stdin.readline())
mode=sys.argv[1]
if mode == "timeout":
    time.sleep(60)
if mode == "missing":
    raise SystemExit(0)
trace=AgentTrace("owned test worker",metadata={"synthetic":True,"output":request["input"] if mode != "wrong" else "wrong"})
time.sleep(0.05 if mode == "baseline" else 0.001)
trace.finish()
if mode == "baseline":
    trace.add_event(AgentEvent(event_id="retry",run_id=trace.run_id,event_type="retry",name="repair_format",started_at=trace.started_at,ended_at=trace.ended_at,duration_ms=trace.elapsed_ms))
trace.export_json(request["trace_path"])
if mode == "failure":
    raise SystemExit(1)
if mode == "cancelled":
    raise SystemExit(130)
"""


@pytest.fixture
def workload(tmp_path):
    path = tmp_path / "tasks.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "name": "Owned test tasks",
                "repetitions": 2,
                "tasks": [
                    {"id": "first", "input": "one", "expected": "one"},
                    {"id": "second", "input": "two", "expected": "two"},
                ],
            }
        ),
        encoding="utf-8",
    )
    worker = tmp_path / "worker.py"
    worker.write_text(WORKER, encoding="utf-8")
    return path, worker


def command(worker, mode):
    return [sys.executable, str(worker), mode]


def prepare(tmp_path, workload):
    tasks, worker = workload
    capture(tasks, tmp_path / "baseline", command(worker, "baseline"))
    start(tasks, tmp_path / "baseline/capture.json", tmp_path / "session")
    return tasks, worker


def test_guided_workflow_uses_native_quality_replay_and_studies(tmp_path, workload):
    tasks, worker = prepare(tmp_path, workload)
    capture(tasks, tmp_path / "candidate", command(worker, "fast"))
    report = finish(
        tmp_path / "session/session.json",
        tmp_path / "candidate/capture.json",
        tmp_path / "results",
        change="Remove repeated work",
    )
    assert report["passed"] is True
    assert report["planned_attempts"] == report["passed_attempts"] == 4
    assert all(
        row["quality"]["passed"] and row["replay"]["gates"]["passed"] for row in report["cases"]
    )
    assert report["study"] is not None
    assert all(row["intervention"] for row in report["cases"])
    assert (tmp_path / "results/report.html").is_file()
    assert (tmp_path / "session/plan.md").is_file()


@pytest.mark.parametrize("mode", ["wrong", "failure", "missing"])
def test_faster_wrong_failed_or_missing_candidates_are_rejected(tmp_path, workload, mode):
    tasks, worker = prepare(tmp_path, workload)
    capture(tasks, tmp_path / "candidate", command(worker, mode))
    report = finish(
        tmp_path / "session/session.json",
        tmp_path / "candidate/capture.json",
        tmp_path / "results",
        change="Rejected test change",
    )
    assert report["passed"] is False
    assert report["planned_attempts"] == 4
    assert report["passed_attempts"] == 0


def test_task_input_changes_cannot_be_paired(tmp_path, workload):
    tasks, worker = prepare(tmp_path, workload)
    payload = json.loads(tasks.read_text())
    payload["tasks"][0]["input"] = "changed"
    tasks.write_text(json.dumps(payload), encoding="utf-8")
    capture(tasks, tmp_path / "candidate", command(worker, "fast"))
    with pytest.raises(ValueError, match="different task set"):
        finish(
            tmp_path / "session/session.json",
            tmp_path / "candidate/capture.json",
            tmp_path / "results",
            change="Changed inputs",
        )
    assert not (tmp_path / "results").exists()


def test_modified_frozen_baseline_is_rejected_before_writing(tmp_path, workload):
    tasks, worker = prepare(tmp_path, workload)
    capture(tasks, tmp_path / "candidate", command(worker, "fast"))
    source = tmp_path / "session/baseline/0000.json"
    payload = json.loads(source.read_text())
    payload["elapsed_ms"] = 999999
    source.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="Frozen baseline"):
        finish(
            tmp_path / "session/session.json",
            tmp_path / "candidate/capture.json",
            tmp_path / "results",
            change="Tamper test",
        )
    assert not (tmp_path / "results").exists()


def test_candidate_capture_must_follow_the_frozen_baseline(tmp_path, workload):
    tasks, worker = workload
    capture(tasks, tmp_path / "baseline", command(worker, "baseline"))
    capture(tasks, tmp_path / "candidate", command(worker, "fast"))
    start(tasks, tmp_path / "baseline/capture.json", tmp_path / "session")
    with pytest.raises(ValueError, match="after freezing"):
        finish(
            tmp_path / "session/session.json",
            tmp_path / "candidate/capture.json",
            tmp_path / "results",
            change="Late selection",
        )


def test_duplicate_or_reused_run_ids_are_rejected(tmp_path, workload):
    tasks, worker = prepare(tmp_path, workload)
    capture(tasks, tmp_path / "candidate", command(worker, "fast"))
    path = tmp_path / "candidate/capture.json"
    manifest = json.loads(path.read_text())
    manifest["runs"][1] = manifest["runs"][0]
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate"):
        finish(tmp_path / "session/session.json", path, tmp_path / "results", change="Duplicate")


def test_runner_never_receives_expected_answers(tmp_path, workload):
    tasks, worker = workload
    worker.write_text(
        'import json,sys\nr=json.load(sys.stdin)\nassert "expected" not in r\nassert set(r)=={"input","task_id","repetition","trace_path"}\n',
        encoding="utf-8",
    )
    report = capture(tasks, tmp_path / "capture", command(worker, "missing"))
    assert {row["status"] for row in report["runs"]} == {"missing_trace"}


def test_timeout_retains_every_planned_attempt(tmp_path, workload):
    tasks, worker = workload
    report = capture(tasks, tmp_path / "capture", command(worker, "timeout"), timeout=0.1)
    assert len(report["runs"]) == 4
    assert all(row["status"] == "timed_out" for row in report["runs"])


def test_child_interrupt_exit_status_is_retained_as_cancellation(tmp_path, workload):
    tasks, worker = workload
    report = capture(tasks, tmp_path / "capture", command(worker, "cancelled"))
    assert all(row["status"] == "cancelled" for row in report["runs"])


def test_cli_runs_the_normal_entry_points_and_returns_quality_failure(tmp_path, workload):
    tasks, worker = workload
    runner = CliRunner()
    baseline = runner.invoke(
        app,
        [
            "improve",
            "capture",
            "--tasks",
            str(tasks),
            "--out",
            str(tmp_path / "baseline"),
            "--",
            *command(worker, "baseline"),
        ],
    )
    assert baseline.exit_code == 0, baseline.output
    session = runner.invoke(
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
        ],
    )
    assert session.exit_code == 0, session.output
    capture(tasks, tmp_path / "candidate", command(worker, "wrong"))
    result = runner.invoke(
        app,
        [
            "improve",
            "finish",
            "--session",
            str(tmp_path / "session/session.json"),
            "--candidate",
            str(tmp_path / "candidate/capture.json"),
            "--change",
            "Bad candidate",
            "--out",
            str(tmp_path / "results"),
        ],
    )
    assert result.exit_code == 1, result.output
    assert (tmp_path / "results/report.json").is_file()


def test_no_executable_scorer_can_be_loaded_from_the_task_file(tmp_path, workload):
    tasks, _ = workload
    value = json.loads(tasks.read_text())
    value["tasks"][0]["scorer"] = {"type": "custom", "callable": "untrusted:run"}
    tasks.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="built-in"):
        read_workload(tasks)


def test_html_escapes_supplied_change_text(tmp_path, workload):
    tasks, worker = prepare(tmp_path, workload)
    capture(tasks, tmp_path / "candidate", command(worker, "fast"))
    finish(
        tmp_path / "session/session.json",
        tmp_path / "candidate/capture.json",
        tmp_path / "results",
        change='<img src=x onerror="alert(1)">',
    )
    page = (tmp_path / "results/report.html").read_text(encoding="utf-8")
    assert "<img" not in page
    assert "&lt;img" in page


def test_interruption_retains_cancelled_and_unstarted_attempts(tmp_path, workload, monkeypatch):
    from agentloop import improvement

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(improvement, "_invoke", interrupt)
    tasks, worker = workload
    with pytest.raises(KeyboardInterrupt):
        capture(tasks, tmp_path / "capture", command(worker, "baseline"))
    manifest = json.loads((tmp_path / "capture/capture.json").read_bytes())
    assert [row["status"] for row in manifest["runs"]] == [
        "cancelled",
        "not_started",
        "not_started",
        "not_started",
    ]


def test_invalid_trace_keeps_the_runner_failure_status(tmp_path, workload):
    tasks, worker = workload
    worker.write_text(
        "import json,sys\nr=json.load(sys.stdin)\n"
        "open(r['trace_path'],'w').write('not json')\n"
        "raise SystemExit(int(sys.argv[1]))\n",
        encoding="utf-8",
    )
    failed = capture(tasks, tmp_path / "failed", command(worker, "1"))
    assert {row["status"] for row in failed["runs"]} == {"failed"}
    assert all(row["trace"] is None for row in failed["runs"])
    completed = capture(tasks, tmp_path / "completed", command(worker, "0"))
    assert {row["status"] for row in completed["runs"]} == {"invalid_trace"}


def test_rehashed_saved_session_cannot_execute_a_custom_scorer(tmp_path, workload, monkeypatch):
    tasks, worker = prepare(tmp_path, workload)
    capture(tasks, tmp_path / "candidate", command(worker, "fast"))
    calls = []
    canary = ModuleType("guided_scorer_canary")
    canary.score = lambda *args: calls.append(True) or True
    monkeypatch.setitem(sys.modules, "guided_scorer_canary", canary)
    session_path = tmp_path / "session/session.json"
    saved = json.loads(session_path.read_bytes())
    taskset = saved["specification"]["workload"]
    taskset["tasks"][0]["scorer"] = {"type": "custom", "callable": "guided_scorer_canary:score"}
    saved["session_hash"] = fingerprint(saved["specification"])
    session_path.write_text(json.dumps(saved), encoding="utf-8")
    candidate_path = tmp_path / "candidate/capture.json"
    manifest = json.loads(candidate_path.read_bytes())
    manifest["workload_hash"] = fingerprint(taskset)
    candidate_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="built-in"):
        finish(session_path, candidate_path, tmp_path / "results", change="Untrusted saved scorer")
    assert calls == []
    assert not (tmp_path / "results").exists()


@pytest.mark.parametrize("mutation", ["omit", "duplicate", "version"])
def test_rehashed_sessions_still_validate_the_full_attempt_inventory(tmp_path, workload, mutation):
    tasks, worker = prepare(tmp_path, workload)
    capture(tasks, tmp_path / "candidate", command(worker, "fast"))
    path = tmp_path / "session/session.json"
    saved = json.loads(path.read_bytes())
    specification = saved["specification"]
    if mutation == "omit":
        specification["cases"].pop()
    elif mutation == "duplicate":
        specification["cases"][1] = specification["cases"][0]
    else:
        specification["schema_version"] = "999.0"
    saved["session_hash"] = fingerprint(specification)
    path.write_text(json.dumps(saved), encoding="utf-8")
    with pytest.raises(ValueError, match="frozen|Frozen"):
        finish(
            path,
            tmp_path / "candidate/capture.json",
            tmp_path / "results",
            change="Invalid inventory",
        )
    assert not (tmp_path / "results").exists()
