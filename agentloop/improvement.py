"""Capture and freeze a task set, then compare a developer-applied change."""

from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import subprocess  # nosec B404 - execution requires explicit CLI argv
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from agentloop.experiment_artifacts import local_path, read_json, write_once
from agentloop.experiment_types import fingerprint
from agentloop.findings import build_diagnosis
from agentloop.interventions import build_intervention
from agentloop.quality import build_quality_report, validate_quality_fixtures
from agentloop.replay import ReplayGates, build_replay_report
from agentloop.tracer import AgentTrace

VERSION = "1.0"
SCORERS = {"exact_match", "contains", "json_subset", "required_fields", "glob"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def now():
    return datetime.now(timezone.utc).isoformat()


def load_json(path):
    path = Path(path)
    require(
        path.is_file() and path.stat().st_size <= 64 * 1024 * 1024,
        "Expected a JSON file of at most 64 MiB",
    )
    value = read_json(path)
    require(isinstance(value, dict), "Expected a JSON object")
    fingerprint(value)  # Reject non-finite JSON before it enters a frozen artifact.
    return value


def read_workload(path):
    value = load_json(path)
    require(
        isinstance(value, dict) and value.get("schema_version") == VERSION,
        "Workload schema_version must be 1.0",
    )
    require(
        not set(value) - {"schema_version", "name", "repetitions", "tasks"},
        "Unknown workload field",
    )
    require(
        isinstance(value.get("name"), str) and bool(value["name"].strip()),
        "Workload name is required",
    )
    tasks, repetitions = value.get("tasks"), value.get("repetitions", 1)
    require(type(repetitions) is int and 1 <= repetitions <= 20, "Use 1 to 20 repetitions")
    require(
        isinstance(tasks, list) and 0 < len(tasks) * repetitions <= 1000,
        "Use 1 to 1000 planned task attempts",
    )
    identities = set()
    for task in tasks:
        require(
            isinstance(task, dict) and not set(task) - {"id", "input", "expected", "scorer"},
            "Unknown task field",
        )
        identity = task.get("id")
        require(
            isinstance(identity, str) and bool(identity.strip()) and identity not in identities,
            "Task IDs must be nonempty and unique",
        )
        require(
            "input" in task and "expected" in task,
            "Each task needs input and an independent expected value",
        )
        scorer = task.get("scorer", {"type": "exact_match"})
        require(
            isinstance(scorer, dict) and scorer.get("type", "exact_match") in SCORERS,
            "Use a built-in deterministic quality scorer",
        )
        validate_quality_fixtures(
            [{"id": identity, "expected": task["expected"], "scorer": scorer}]
        )
        identities.add(identity)
    return {**value, "repetitions": repetitions}


def slots(workload):
    return [
        (task, repetition)
        for task in workload["tasks"]
        for repetition in range(workload["repetitions"])
    ]


def trace_receipt(root, relative):
    path = local_path(root, relative)
    require(
        path.is_file() and path.stat().st_size <= 64 * 1024 * 1024,
        "Trace exceeds 64 MiB or is not a regular file",
    )
    with path.open("rb") as handle:
        data = handle.read(64 * 1024 * 1024 + 1)
    require(len(data) <= 64 * 1024 * 1024, "Trace exceeds 64 MiB")
    trace = AgentTrace.from_dict(json.loads(data))
    require(
        trace.ended_at is not None and trace.elapsed_ms is not None,
        "Trace must have a completed timing boundary",
    )
    return trace, hashlib.sha256(data).hexdigest()


def _stop_process(process):
    if os.name == "nt":
        subprocess.run(  # nosec B603 B607 - only the owned child process tree
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=10)


def _invoke(command, request, timeout, *, show_output=False):
    options = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    try:
        process = subprocess.Popen(  # nosec B603 - explicit operator-supplied argv, never loaded from JSON
            command,
            stdin=subprocess.PIPE,
            stdout=None if show_output else subprocess.DEVNULL,
            stderr=None if show_output else subprocess.DEVNULL,
            **options,
        )
    except OSError:
        return "runner_error"
    try:
        process.communicate((json.dumps(request) + "\n").encode(), timeout=timeout)
        return "completed" if process.returncode == 0 else "failed"
    except subprocess.TimeoutExpired:
        _stop_process(process)
        return "timed_out"
    except BaseException:
        _stop_process(process)
        raise


def capture(workload_path, root, command, *, timeout=180, show_output=False):
    workload = read_workload(workload_path)
    require(
        command and all(isinstance(arg, str) and arg for arg in command),
        "Supply an explicit runner command after --",
    )
    require(
        type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 3600,
        "Task timeout must be positive and at most 3600 seconds",
    )
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    write_once(root, "workload.json", workload)
    scripts = [
        {"argument_index": index, "sha256": hashlib.sha256(Path(argument).read_bytes()).hexdigest()}
        for index, argument in enumerate(command)
        if Path(argument).suffix in {".py", ".js", ".mjs", ".ts"} and Path(argument).is_file()
    ]
    manifest = {
        "schema_version": VERSION,
        "created_at": now(),
        "workload_hash": fingerprint(workload),
        "runner_hash": fingerprint({"argv": command, "scripts": scripts}),
        "runs": [],
    }
    for index, (task, repetition) in enumerate(slots(workload)):
        manifest["runs"].append(
            {
                "task_id": task["id"],
                "repetition": repetition,
                "status": "not_started",
                "input_hash": fingerprint(task["input"]),
                "trace": None,
                "trace_sha256": None,
            }
        )
    try:
        for index, row in enumerate(manifest["runs"]):
            task = next(task for task in workload["tasks"] if task["id"] == row["task_id"])
            relative = f"traces/{index:04d}.json"
            path = local_path(root, relative)
            path.parent.mkdir(exist_ok=True)
            request = {
                "task_id": row["task_id"],
                "repetition": row["repetition"],
                "input": task["input"],
                "trace_path": str(path),
            }
            row["started_at"] = now()
            try:
                row["status"] = _invoke(command, request, timeout, show_output=show_output)
            except KeyboardInterrupt:
                row["status"] = "cancelled"
                raise
            finally:
                row["ended_at"] = now()
                if path.exists():
                    try:
                        _, digest = trace_receipt(root, relative)
                        row.update(trace=relative, trace_sha256=digest)
                    except (ValueError, OSError, TypeError, KeyError, AttributeError):
                        # Keep the more specific failed, timed-out or cancelled outcome.
                        if row["status"] == "completed":
                            row["status"] = "invalid_trace"
                elif row["status"] == "completed":
                    row["status"] = "missing_trace"
                write_once(root, f"receipts/{index:04d}.json", row)
    finally:
        write_once(root, "capture.json", manifest)
    return manifest


def read_capture(path, workload):
    path = Path(path).resolve()
    manifest = load_json(path)
    require(
        manifest.get("schema_version") == VERSION
        and manifest.get("workload_hash") == fingerprint(workload),
        "Capture belongs to a different task set",
    )
    planned = {(task["id"], repetition): task for task, repetition in slots(workload)}
    runs, identities = manifest.get("runs"), set()
    require(
        isinstance(runs, list) and len(runs) == len(planned),
        "Capture must retain every planned attempt",
    )
    traces, run_ids = {}, set()
    for row in runs:
        key = (row["task_id"], row["repetition"])
        require(
            key in planned
            and key not in identities
            and row["input_hash"] == fingerprint(planned[key]["input"]),
            "Duplicate, unknown or changed task attempt",
        )
        require(
            row["status"]
            in {
                "completed",
                "failed",
                "timed_out",
                "runner_error",
                "invalid_trace",
                "missing_trace",
                "cancelled",
                "not_started",
            },
            "Unknown capture status",
        )
        identities.add(key)
        if row["trace"] is not None:
            trace, digest = trace_receipt(path.parent, row["trace"])
            require(
                digest == row["trace_sha256"] and trace.run_id not in run_ids,
                "Changed trace bytes or reused run identity",
            )
            run_ids.add(trace.run_id)
            traces[key] = trace
        elif row["status"] == "completed":
            raise ValueError("Completed attempt has no trace")
    return manifest, traces


def _quality(task, baseline, candidate):
    return build_quality_report(
        [
            {
                "id": task["id"],
                "expected": task["expected"],
                "scorer": task.get("scorer", {"type": "exact_match"}),
            }
        ],
        baseline_trace=baseline,
        candidate_trace=candidate,
        min_score=1.0,
    )


def start(
    workload_path, baseline_path, root, *, focus=None, objective="latency", minimum_improvement=0
):
    workload = read_workload(workload_path)
    manifest, traces = read_capture(baseline_path, workload)
    require(objective in {"latency", "cost"}, "Objective must be latency or cost")
    require(
        type(minimum_improvement) in (int, float)
        and math.isfinite(minimum_improvement)
        and minimum_improvement >= 0,
        "Minimum improvement must be finite and nonnegative",
    )
    diagnoses = {key: build_diagnosis(trace) for key, trace in traces.items()}
    findings = [finding for diagnosis in diagnoses.values() for finding in diagnosis["findings"]]
    if focus is None and findings:
        focus = findings[0]["type"]
    require(
        focus is None or any(item["type"] == focus for item in findings),
        "The selected finding type is absent from the baseline",
    )
    gates = ReplayGates(
        min_quality_score=1.0,
        min_latency_improvement_pct=minimum_improvement if objective == "latency" else 0,
        min_cost_improvement_pct=minimum_improvement if objective == "cost" else 0,
    )
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    cases = []
    for index, (task, repetition) in enumerate(slots(workload)):
        key = (task["id"], repetition)
        trace, diagnosis = traces.get(key), diagnoses.get(key)
        row = next(row for row in manifest["runs"] if (row["task_id"], row["repetition"]) == key)
        relative = f"baseline/{index:04d}.json" if trace else None
        if trace:
            write_once(root, relative, trace.to_dict())
        cases.append(
            {
                "task_id": task["id"],
                "repetition": repetition,
                "capture_status": row["status"],
                "trace": relative,
                "trace_hash": fingerprint(trace.to_dict()) if trace else None,
                "diagnosis": diagnosis,
                "finding_ids": [
                    finding["finding_id"]
                    for finding in diagnosis["findings"]
                    if finding["type"] == focus
                ]
                if diagnosis
                else [],
            }
        )
    specification = {
        "schema_version": VERSION,
        "frozen_at": now(),
        "workload": workload,
        "focus": focus,
        "objective": objective,
        "gates": asdict(gates),
        "cases": cases,
        "baseline_capture": manifest,
    }
    write_once(
        root,
        "session.json",
        {"session_hash": fingerprint(specification), "specification": specification},
    )
    from agentloop.improvement_report import plan_markdown

    (root / "plan.md").write_text(plan_markdown(specification), encoding="utf-8")
    return specification


def _trace_usable(trace):
    if trace.metadata.get("success") is False:
        return False
    if trace.metadata.get("integration") == "hermes":
        capture = trace.metadata.get("hermes", {})
        if not (
            isinstance(capture, dict)
            and capture.get("capture_complete") is True
            and capture.get("turn_outcome") == "completed"
            and capture.get("unfinished_requests", 0) == 0
            and capture.get("events_dropped", 0) == 0
        ):
            return False
    return not any(event.parent_id is None and event.status == "error" for event in trace.events)


def finish(session_path, candidate_path, root, *, change):
    session_path = Path(session_path).resolve()
    saved = load_json(session_path)
    specification = saved["specification"]
    require(saved["session_hash"] == fingerprint(specification), "Frozen session was modified")
    require(
        isinstance(change, str) and bool(change.strip()), "Describe the developer-applied change"
    )
    workload = specification["workload"]
    candidate_manifest, candidates = read_capture(candidate_path, workload)
    require(
        datetime.fromisoformat(candidate_manifest["created_at"])
        >= datetime.fromisoformat(specification["frozen_at"]),
        "Capture candidates after freezing baseline findings",
    )
    baseline_ids = set()
    baselines = {}
    for case in specification["cases"]:
        key = (case["task_id"], case["repetition"])
        if case["trace"]:
            trace = AgentTrace.from_dict(load_json(local_path(session_path.parent, case["trace"])))
            require(
                fingerprint(trace.to_dict()) == case["trace_hash"], "Frozen baseline trace changed"
            )
            baseline_ids.add(trace.run_id)
            baselines[key] = trace
    require(
        not baseline_ids.intersection(trace.run_id for trace in candidates.values()),
        "Baseline and candidate must have distinct run IDs",
    )
    gates = ReplayGates(**specification["gates"])
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    rows, study_paths = [], {"baseline": [], "candidate": []}
    for index, case in enumerate(specification["cases"]):
        key = (case["task_id"], case["repetition"])
        task = next(task for task in workload["tasks"] if task["id"] == key[0])
        baseline, candidate = baselines.get(key), candidates.get(key)
        candidate_row = next(
            row for row in candidate_manifest["runs"] if (row["task_id"], row["repetition"]) == key
        )
        row = {
            "task_id": key[0],
            "repetition": key[1],
            "baseline_status": case["capture_status"],
            "candidate_status": candidate_row["status"],
            "passed": False,
            "reasons": [],
            "quality": None,
            "replay": None,
            "intervention": None,
        }
        if baseline is None or candidate is None:
            row["reasons"].append("A planned baseline or candidate trace is unavailable")
        else:
            quality = _quality(task, baseline, candidate)
            replay = build_replay_report(baseline, candidate, gates=gates, quality_report=quality)
            if (
                case["capture_status"] != "completed"
                or candidate_row["status"] != "completed"
                or not all(_trace_usable(trace) for trace in (baseline, candidate))
            ):
                row["reasons"].append("An attempt failed, was interrupted or did not complete")
            if not quality["passed"] or any(
                not result[side]["passed"]
                for result in quality["cases"]
                for side in ("baseline", "candidate")
            ):
                row["reasons"].append(
                    "Baseline and candidate must both pass the declared quality checks"
                )
            if not replay["gates"]["passed"]:
                row["reasons"].append("Native replay gates failed")
            if not case["diagnosis"].get("analysis_complete", True) or not candidate.report().get(
                "analysis_complete", True
            ):
                row["reasons"].append("Finding analysis is incomplete")
            gain = replay["deltas"][
                "latency_improvement_pct"
                if specification["objective"] == "latency"
                else "cost_improvement_pct"
            ]
            if gain is None or gain <= 0:
                row["reasons"].append("The selected objective has no measured improvement")
            row.update(quality=quality, replay=replay, passed=not row["reasons"])
            if case["finding_ids"]:
                record = build_intervention(
                    baseline,
                    candidate,
                    target_finding_ids=case["finding_ids"],
                    intervention_type=specification["focus"],
                    configuration={"change": change},
                    diagnosis=case["diagnosis"],
                    gates=gates,
                    quality_report=quality,
                    replay_report=replay,
                )
                row["intervention"] = record.to_dict()
        for side, trace in (("baseline", baseline), ("candidate", candidate)):
            if trace is not None:
                write_once(root, f"traces/{side}/{index:04d}.json", trace.to_dict())
                viewed = trace.to_dict()
                viewed["metadata"].update(
                    task_id=key[0],
                    repetition=key[1],
                    source_trace_hash=fingerprint(trace.to_dict()),
                )
                if row["quality"]:
                    result = row["quality"]["cases"][0][side]
                    viewed["metadata"].update(
                        quality_score=result["score"],
                        success=result["passed"]
                        and _trace_usable(trace)
                        and row[f"{side}_status"] == "completed",
                    )
                relative = f"study/{side}/{index:04d}.json"
                write_once(root, relative, viewed)
                study_paths[side].append(relative)
        rows.append(row)
    study = None
    if all(study_paths.values()):
        from agentloop.studies import summarize_study

        study_manifest = {
            "schema_version": VERSION,
            "name": workload["name"],
            "baseline": "baseline",
            "conditions": study_paths,
            "pairing_keys": ["task_id", "repetition"],
        }
        study_file = write_once(root, "study.json", study_manifest)
        study = summarize_study(study_file)
    report = {
        "schema_version": VERSION,
        "name": workload["name"],
        "session_hash": saved["session_hash"],
        "objective": specification["objective"],
        "change": change,
        "focus": specification["focus"],
        "workload_hash": fingerprint(workload),
        "baseline_capture_hash": fingerprint(specification["baseline_capture"]),
        "candidate_capture_hash": fingerprint(candidate_manifest),
        "baseline_runner_hash": specification["baseline_capture"].get("runner_hash"),
        "candidate_runner_hash": candidate_manifest.get("runner_hash"),
        "planned_attempts": len(rows),
        "passed_attempts": sum(row["passed"] for row in rows),
        "passed": all(row["passed"] for row in rows),
        "synthetic": any(
            trace.metadata.get("synthetic") is True
            for trace in [*baselines.values(), *candidates.values()]
        ),
        "cases": rows,
        "study": study,
    }
    from agentloop.improvement_report import export_report

    export_report(root, report)
    return report
