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
    trace.add_event(AgentEvent(event_id="retry",run_id=trace.run_id,event_type="retry",name="repair_format",started_at=trace.started_at,ended_at=trace.ended_at,duration_ms=trace.elapsed_ms,metadata={"retry_reason":"invalid_output_schema"}))
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
    assert report["timing_summary"]["trace_elapsed_ms"]["paired_count"] == 4
    assert report["timing_summary"]["runner_elapsed_ms"]["paired_count"] == 4
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
    assert report["timing_summary"]["eligible_pairs"] == 0
    assert report["timing_summary"]["runner_elapsed_ms"]["paired_count"] == 0
    assert all(row["timing"]["candidate"]["runner_elapsed_ms"] > 0 for row in report["cases"])
    if mode == "missing":
        assert all(
            row["timing"]["candidate"]["trace_elapsed_ms"] is None for row in report["cases"]
        )
        assert "| Trace latency (ms) |" in (tmp_path / "results/report.md").read_text(
            encoding="utf-8"
        )
    assert all(
        row["intervention"] is None or row["intervention"]["gates_passed"] is False
        for row in report["cases"]
    )


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
    assert "Trace latency (ms)" in result.output
    assert "Runner wall time (ms)" in result.output
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
    assert manifest["runs"][0]["runner_elapsed_ms"] >= 0
    assert all("runner_elapsed_ms" not in row for row in manifest["runs"][1:])


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


def test_interleaved_control_alternates_order_and_becomes_the_comparison(tmp_path, workload):
    tasks, worker = prepare(tmp_path, workload)
    manifest = capture(
        tasks,
        tmp_path / "paired",
        command(worker, "fast"),
        control=command(worker, "baseline"),
    )
    control = json.loads((tmp_path / "paired/control/capture.json").read_bytes())
    assert manifest["control"]["manifest_hash"] == fingerprint(control)
    assert [row["order"] for row in control["runs"]] == [0, 1, 0, 1]
    assert [row["order"] for row in manifest["runs"]] == [1, 0, 1, 0]
    for index, (first, second) in enumerate(zip(control["runs"], manifest["runs"])):
        ordered = (first, second) if index % 2 == 0 else (second, first)
        assert ordered[0]["ended_at"] <= ordered[1]["started_at"]
    report = finish(
        tmp_path / "session/session.json",
        tmp_path / "paired/capture.json",
        tmp_path / "results",
        change="Remove repeated work",
    )
    assert report["passed"] is True
    assert report["comparison_baseline"] == "interleaved_control"
    assert report["control_capture_hash"] == fingerprint(control)
    control_ids = {
        json.loads((tmp_path / "paired/control" / row["trace"]).read_bytes())["run_id"]
        for row in control["runs"]
    }
    for row in report["cases"]:
        assert row["replay"]["baseline"]["run_id"] in control_ids
        assert row["intervention"]["baseline_run_id"] in control_ids
        assert row["frozen_baseline_status"] == "completed"
    assert [row["control_ran_first"] for row in report["cases"]] == [True, False, True, False]
    for index, row in enumerate(report["cases"]):
        timing = row["timing"]
        assert (
            timing["baseline"]["runner_elapsed_ms"] == control["runs"][index]["runner_elapsed_ms"]
        )
        assert timing["baseline"]["runner_clock"] == "monotonic"
        assert (
            timing["candidate"]["runner_elapsed_ms"] == manifest["runs"][index]["runner_elapsed_ms"]
        )
    markdown = (tmp_path / "results/report.md").read_text(encoding="utf-8")
    assert "alternating order" in markdown
    assert "| Measurement | Control | Candidate |" in markdown
    page = (tmp_path / "results/report.html").read_text(encoding="utf-8")
    assert "Control median" in page and "Control trace" in page
    assert "Trace latency (ms)" in page and "Runner wall time (ms)" in page


def test_timing_summary_reads_legacy_receipts_and_keeps_runner_time_out_of_gates(
    tmp_path, workload
):
    tasks, worker = workload
    baseline_path = tmp_path / "baseline/capture.json"
    baseline = capture(tasks, baseline_path.parent, command(worker, "baseline"))
    for row, end in zip(baseline["runs"], ("00.150", "00.350", "00.200", "00.099"), strict=True):
        row.pop("runner_elapsed_ms")
        row.update(started_at="2026-01-01T00:00:00.100Z", ended_at=f"2026-01-01T00:00:{end}+00:00")
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    session = tmp_path / "session"
    start(tasks, baseline_path, session)

    candidate_path = tmp_path / "candidate/capture.json"
    candidate = capture(tasks, candidate_path.parent, command(worker, "fast"))
    for row, runner_ms in zip(candidate["runs"], (300, 350, True, 400), strict=True):
        row["runner_elapsed_ms"] = runner_ms
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")

    report = finish(
        session / "session.json",
        candidate_path,
        tmp_path / "results",
        change="Spend less time inside the trace",
    )
    assert report["passed"]
    assert "runner_gate" not in report
    assert report["timing_summary"]["eligible_pairs"] == 4
    assert report["timing_summary"]["trace_elapsed_ms"]["paired_count"] == 4
    assert report["timing_summary"]["runner_elapsed_ms"] == {
        "paired_count": 2,
        "baseline_median_ms": 150.0,
        "candidate_median_ms": 325.0,
        "median_paired_delta_ms": 175.0,
    }
    assert report["cases"][0]["timing"]["baseline"]["runner_clock"] == "capture_timestamps"
    assert report["cases"][2]["timing"]["candidate"]["runner_clock"] is None
    assert report["cases"][3]["timing"]["baseline"]["runner_elapsed_ms"] is None


@pytest.mark.parametrize("decision", ["per-pair", "aggregate"])
def test_runner_wall_gate_uses_interleaved_control_and_rejects_unavailable_measurements(
    tmp_path, workload, decision
):
    tasks, worker = workload
    baseline_path = tmp_path / "baseline/capture.json"
    baseline = capture(tasks, baseline_path.parent, command(worker, "baseline"))
    for row in baseline["runs"]:
        row["runner_elapsed_ms"] = 1000
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    session = tmp_path / "session"
    cli = CliRunner()
    initialized = cli.invoke(
        app,
        [
            "improve",
            "start",
            "--tasks",
            str(tasks),
            "--baseline",
            str(baseline_path),
            "--out",
            str(session),
            "--max-runner-regression-ms",
            "30",
            "--decision",
            decision,
        ],
    )
    assert initialized.exit_code == 0, initialized.output
    assert (
        json.loads((session / "session.json").read_bytes())["specification"][
            "max_runner_regression_ms"
        ]
        == 30
    )

    paired = tmp_path / "paired"
    candidate = capture(tasks, paired, command(worker, "fast"), control=command(worker, "baseline"))
    control_path = paired / "control/capture.json"
    control = json.loads(control_path.read_bytes())
    for row in control["runs"]:
        row["runner_elapsed_ms"] = 100
    control_path.write_text(json.dumps(control), encoding="utf-8")
    candidate["control"]["manifest_hash"] = fingerprint(control)
    for row, elapsed in zip(candidate["runs"], (99, 130, 131), strict=False):
        row["runner_elapsed_ms"] = elapsed
    candidate["runs"][3].pop(
        "runner_elapsed_ms"
    )  # A valid legacy timestamp pair stays descriptive.
    candidate_path = paired / "capture.json"
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")

    output = tmp_path / "results"
    result = cli.invoke(
        app,
        [
            "improve",
            "finish",
            "--session",
            str(session / "session.json"),
            "--candidate",
            str(candidate_path),
            "--change",
            "Fast trace with a slow runner",
            "--out",
            str(output),
        ],
    )
    assert result.exit_code == 1, result.output
    assert "Runner wall-time gate passed on 2 of 4 pairs" in result.output
    report = json.loads((output / "report.json").read_bytes())
    assert report["runner_gate"] == {"limit_ms": 30, "passed_pairs": 2, "unavailable_pairs": 1}
    assert report["passed"] is False
    assert [row["runner_gate"]["delta_ms"] for row in report["cases"]] == [-1, 30, 31, None]
    assert all(row["quality"]["passed"] for row in report["cases"])
    for row in report["cases"][2:]:
        assert row["passed"] is False
        assert row["intervention"]["gates_passed"] is False
        assert any(
            gate["name"] == "runner_wall_time_regression" and not gate["passed"]
            for gate in row["replay"]["gates"]["results"]
        )
    assert "Runner wall-time gate passed on 2 of 4 pairs" in (output / "report.html").read_text(
        encoding="utf-8"
    )


def test_interleaved_control_must_also_pass_quality_checks(tmp_path, workload):
    tasks, worker = prepare(tmp_path, workload)
    capture(
        tasks,
        tmp_path / "paired",
        command(worker, "fast"),
        control=command(worker, "wrong"),
    )
    report = finish(
        tmp_path / "session/session.json",
        tmp_path / "paired/capture.json",
        tmp_path / "results",
        change="Wrong control",
    )
    assert report["passed"] is False
    assert all(
        any(reason.startswith("Control answer failed") for reason in row["reasons"])
        for row in report["cases"]
    )


def test_modified_interleaved_control_is_rejected(tmp_path, workload):
    tasks, worker = prepare(tmp_path, workload)
    capture(
        tasks,
        tmp_path / "paired",
        command(worker, "fast"),
        control=command(worker, "baseline"),
    )
    path = tmp_path / "paired/control/capture.json"
    control = json.loads(path.read_bytes())
    control["runner_hash"] = "changed"
    path.write_text(json.dumps(control), encoding="utf-8")
    with pytest.raises(ValueError, match="control capture changed"):
        finish(
            tmp_path / "session/session.json",
            tmp_path / "paired/capture.json",
            tmp_path / "results",
            change="Tampered control",
        )
    assert not (tmp_path / "results").exists()


def test_cli_accepts_repeated_control_arguments(tmp_path, workload):
    tasks, worker = workload
    result = CliRunner().invoke(
        app,
        [
            "improve",
            "capture",
            "--tasks",
            str(tasks),
            "--out",
            str(tmp_path / "paired"),
            *[item for arg in command(worker, "baseline") for item in ("--control-arg", arg)],
            "--",
            *command(worker, "fast"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Captured 8 of 8 planned attempts." in result.output
    assert (tmp_path / "paired/control/capture.json").is_file()


NOISY = """import json,sys,time
from agentloop.tracer import AgentTrace
request=json.loads(sys.stdin.readline())
delays={0:0.12,1:0.12,2:0.12,3:0.12}
fast={0:0.02,1:0.02,2:0.02,3:0.2}
index={"first":0,"second":2}[request["task_id"]]+request["repetition"]
trace=AgentTrace("noisy worker",metadata={"synthetic":True,"output":request["input"]})
time.sleep((fast if sys.argv[1]=="candidate" else delays)[index])
trace.finish()
trace.export_json(request["trace_path"])
"""


def noisy(tmp_path, workload, decision):
    tasks, _ = workload
    worker = tmp_path / "noisy.py"
    worker.write_text(NOISY, encoding="utf-8")
    capture(tasks, tmp_path / "baseline", [sys.executable, str(worker), "baseline"])
    start(tasks, tmp_path / "baseline/capture.json", tmp_path / "session", decision=decision)
    capture(tasks, tmp_path / "candidate", [sys.executable, str(worker), "candidate"])
    return finish(
        tmp_path / "session/session.json",
        tmp_path / "candidate/capture.json",
        tmp_path / "results",
        change="Mostly faster with one slow outlier",
    )


def test_per_pair_decision_rejects_a_single_slower_pair(tmp_path, workload):
    report = noisy(tmp_path, workload, "per_pair")
    assert report["decision"] == {"mode": "per_pair", "passed": True}
    assert report["passed"] is False
    assert report["passed_attempts"] == 3


def test_aggregate_decision_accepts_a_median_improvement(tmp_path, workload):
    report = noisy(tmp_path, workload, "aggregate")
    verdict = report["decision"]
    assert report["passed"] is True, verdict
    assert report["passed_attempts"] == 4
    assert verdict["mode"] == "aggregate"
    assert (verdict["improved_pairs"], verdict["regressed_pairs"]) == (3, 1)
    assert verdict["median_improvement_pct"] > 0
    assert verdict["sign_test_p_value"] == pytest.approx(5 / 16)
    assert "judged across all pairs" in (tmp_path / "results/report.md").read_text(encoding="utf-8")


def test_aggregate_decision_still_requires_every_answer_to_pass(tmp_path, workload):
    tasks, worker = workload
    capture(tasks, tmp_path / "baseline", command(worker, "baseline"))
    start(tasks, tmp_path / "baseline/capture.json", tmp_path / "session", decision="aggregate")
    capture(tasks, tmp_path / "candidate", command(worker, "wrong"))
    report = finish(
        tmp_path / "session/session.json",
        tmp_path / "candidate/capture.json",
        tmp_path / "results",
        change="Faster but wrong",
    )
    assert report["passed"] is False
    assert report["passed_attempts"] == 0
    assert report["decision"]["median_improvement_pct"] > 0


def test_aggregate_decision_rejects_a_median_slowdown(tmp_path, workload):
    tasks, _ = workload
    worker = tmp_path / "noisy.py"
    worker.write_text(NOISY, encoding="utf-8")
    # Reverse the roles: three pairs slow down and one speeds up.
    capture(tasks, tmp_path / "baseline", [sys.executable, str(worker), "candidate"])
    start(tasks, tmp_path / "baseline/capture.json", tmp_path / "session", decision="aggregate")
    capture(tasks, tmp_path / "candidate", [sys.executable, str(worker), "baseline"])
    report = finish(
        tmp_path / "session/session.json",
        tmp_path / "candidate/capture.json",
        tmp_path / "results",
        change="Mostly slower",
    )
    verdict = report["decision"]
    assert report["passed_attempts"] == 4
    assert report["passed"] is False
    assert (verdict["improved_pairs"], verdict["regressed_pairs"]) == (1, 3)
    assert any("Median latency" in reason for reason in verdict["reasons"])
    assert any("more pairs must improve" in reason for reason in verdict["reasons"])


def test_decision_is_frozen_with_the_session(tmp_path, workload):
    tasks, worker = prepare(tmp_path, workload)
    path = tmp_path / "session/session.json"
    saved = json.loads(path.read_bytes())
    assert saved["specification"]["decision"] == "per_pair"
    saved["specification"]["decision"] = "aggregate"
    path.write_text(json.dumps(saved), encoding="utf-8")
    capture(tasks, tmp_path / "candidate", command(worker, "fast"))
    with pytest.raises(ValueError, match="Frozen session was modified"):
        finish(path, tmp_path / "candidate/capture.json", tmp_path / "results", change="Late")


def test_unknown_decision_is_rejected(tmp_path, workload):
    tasks, worker = workload
    capture(tasks, tmp_path / "baseline", command(worker, "baseline"))
    with pytest.raises(ValueError, match="per-pair or aggregate"):
        start(tasks, tmp_path / "baseline/capture.json", tmp_path / "session", decision="any")


def test_cli_freezes_the_aggregate_decision(tmp_path, workload):
    tasks, worker = workload
    capture(tasks, tmp_path / "baseline", command(worker, "baseline"))
    result = CliRunner().invoke(
        app,
        [
            "improve",
            "start",
            "--tasks",
            str(tasks),
            "--baseline",
            str(tmp_path / "baseline/capture.json"),
            "--decision",
            "aggregate",
            "--out",
            str(tmp_path / "session"),
        ],
    )
    assert result.exit_code == 0, result.output
    saved = json.loads((tmp_path / "session/session.json").read_bytes())
    assert saved["specification"]["decision"] == "aggregate"
    assert "median paired improvement" in (tmp_path / "session/plan.md").read_text(encoding="utf-8")


def test_interleaved_control_must_follow_the_frozen_session(tmp_path, workload):
    tasks, worker = prepare(tmp_path, workload)
    capture(
        tasks,
        tmp_path / "paired",
        command(worker, "fast"),
        control=command(worker, "baseline"),
    )
    control_path = tmp_path / "paired/control/capture.json"
    control = json.loads(control_path.read_bytes())
    control["created_at"] = "2000-01-01T00:00:00+00:00"
    control_path.write_text(json.dumps(control), encoding="utf-8")
    candidate_path = tmp_path / "paired/capture.json"
    candidate = json.loads(candidate_path.read_bytes())
    candidate["control"]["manifest_hash"] = fingerprint(control)
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    with pytest.raises(ValueError, match="control after freezing"):
        finish(
            tmp_path / "session/session.json",
            candidate_path,
            tmp_path / "results",
            change="Stale control",
        )
    assert not (tmp_path / "results").exists()


def test_cli_output_survives_unicode_names_on_a_legacy_code_page(tmp_path):
    import os
    import subprocess

    tasks = tmp_path / "tasks.json"
    tasks.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "name": "Unicode ✓ 检查",
                "repetitions": 1,
                "tasks": [{"id": "检查-✓", "input": "one", "expected": "one"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from agentloop.entrypoint import app; app()",
            "improve",
            "capture",
            "--tasks",
            str(tasks),
            "--out",
            str(tmp_path / "capture"),
            "--",
            sys.executable,
            str(tmp_path / "missing.py"),
        ],
        capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": "cp1252", "COLUMNS": "200"},
        timeout=120,
        check=False,
    )
    output = result.stdout.decode("utf-8")
    assert result.returncode == 1, result.stderr.decode("utf-8", "replace")
    assert "UnicodeEncodeError" not in result.stderr.decode("utf-8", "replace")
    assert "检查-✓ repetition 0" in output
