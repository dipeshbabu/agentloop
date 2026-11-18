"""Capture and freeze a task set, then compare a developer-applied change."""

from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import statistics
import subprocess  # nosec B404 - execution requires explicit CLI argv
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter_ns

from agentloop.experiment_artifacts import local_path, read_json, write_once
from agentloop.experiment_types import fingerprint
from agentloop.findings import build_diagnosis
from agentloop.improvement_verifier import DEFAULT_TIMEOUT, LocalVerifier, resume_verifier
from agentloop.interventions import build_intervention
from agentloop.quality import build_quality_report, validate_quality_fixtures
from agentloop.repetitions import add_repetition_findings
from agentloop.replay import ReplayGates, build_replay_report
from agentloop.replay import _summary as replay_summary
from agentloop.schema import TraceValidationError, validate_elapsed_ms
from agentloop.tracer import AgentTrace

VERSION = "1.0"
DECISIONS = {"per_pair", "aggregate"}
# Speed and cost gates; an aggregate decision judges these across all pairs.
PERFORMANCE_GATES = {
    "latency_regression",
    "latency_improvement",
    "cost_regression",
    "cost_improvement",
}
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
    return validate_workload(load_json(path))


def validate_workload(value):
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
    except OSError as error:
        return {
            "status": "runner_error",
            "exit_code": None,
            "diagnostic": f"Cannot start {Path(command[0]).name} ({type(error).__name__}). Verify the executable and Python environment.",
        }
    try:
        process.communicate((json.dumps(request) + "\n").encode(), timeout=timeout)
        if process.returncode in {130, -signal.SIGINT, 0xC000013A, -1073741510}:
            return {
                "status": "cancelled",
                "exit_code": process.returncode,
                "diagnostic": "The runner was interrupted. Capture a completed attempt before comparing performance.",
            }
        return {
            "status": "completed" if process.returncode == 0 else "failed",
            "exit_code": process.returncode,
            "diagnostic": None
            if process.returncode == 0
            else f"Runner exited with code {process.returncode}. Rerun capture with --show-runner-output to inspect its error.",
        }
    except subprocess.TimeoutExpired:
        _stop_process(process)
        return {
            "status": "timed_out",
            "exit_code": process.returncode,
            "diagnostic": f"Runner exceeded the {timeout:g}-second task timeout. Inspect the agent, or choose an appropriate --timeout for this workload.",
        }
    except BaseException:
        _stop_process(process)
        raise


CONTROL_CAPTURE = "control/capture.json"


def _new_manifest(workload, command):
    scripts = [
        {"argument_index": index, "sha256": hashlib.sha256(Path(argument).read_bytes()).hexdigest()}
        for index, argument in enumerate(command)
        if Path(argument).suffix in {".py", ".js", ".mjs", ".ts"} and Path(argument).is_file()
    ]
    return {
        "schema_version": VERSION,
        "created_at": now(),
        "workload_hash": fingerprint(workload),
        "runner_hash": fingerprint({"argv": command, "scripts": scripts}),
        "runs": [
            {
                "task_id": task["id"],
                "repetition": repetition,
                "status": "not_started",
                "input_hash": fingerprint(task["input"]),
                "trace": None,
                "trace_sha256": None,
            }
            for task, repetition in slots(workload)
        ],
    }


def _attempt(root, index, row, task, command, timeout, show_output):
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
    started = perf_counter_ns()
    try:
        row.update(_invoke(command, request, timeout, show_output=show_output))
    except KeyboardInterrupt:
        row["status"] = "cancelled"
        raise
    finally:
        if row["status"] != "runner_error":
            row["runner_elapsed_ms"] = (perf_counter_ns() - started) / 1_000_000
        row["ended_at"] = now()
        if path.exists():
            try:
                _, digest = trace_receipt(root, relative)
                row.update(trace=relative, trace_sha256=digest)
            except (ValueError, OSError, TypeError, KeyError, AttributeError) as error:
                # Keep the more specific failed, timed-out or cancelled outcome.
                if row["status"] == "completed":
                    row["status"] = "invalid_trace"
                row["diagnostic"] = (
                    (row.get("diagnostic") or "")
                    + f" Trace {relative} is invalid: {error}. Export a finalized native trace to request['trace_path']."
                )
        elif row["status"] == "completed":
            row["status"] = "missing_trace"
            row["diagnostic"] = (
                "Runner exited successfully without a trace. Export the finalized trace to request['trace_path']."
            )
        write_once(root, f"receipts/{index:04d}.json", row)


def capture(workload_path, root, command, *, timeout=180, show_output=False, control=None):
    """Run every planned attempt, optionally alternating with an unchanged control.

    With ``control``, each task attempt runs both the control and the candidate
    back to back, and the condition that goes first alternates between attempts.
    Cache warm-up, host load and other drift then affect both conditions instead
    of favouring whichever condition ran later.
    """
    workload = read_workload(workload_path)
    require(
        command and all(isinstance(arg, str) and arg for arg in command),
        "Supply an explicit runner command after --",
    )
    require(
        control is None or (control and all(isinstance(arg, str) and arg for arg in control)),
        "Supply the unchanged control runner as repeated --control-arg values",
    )
    require(
        type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 3600,
        "Task timeout must be positive and at most 3600 seconds",
    )
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    write_once(root, "workload.json", workload)
    manifest = _new_manifest(workload, command)
    conditions = [(root, manifest, command)]
    control_manifest = None
    if control is not None:
        control_root = local_path(root, "control")
        control_root.mkdir()
        write_once(control_root, "workload.json", workload)
        control_manifest = _new_manifest(workload, control)
        control_manifest["schedule"] = {"mode": "interleaved", "role": "control"}
        manifest["schedule"] = {
            "mode": "interleaved",
            "role": "candidate",
            "pattern": "Control runs first on even attempt indexes and second on odd ones",
        }
        conditions.append((control_root, control_manifest, control))
    try:
        for index, row in enumerate(manifest["runs"]):
            task = next(task for task in workload["tasks"] if task["id"] == row["task_id"])
            order = conditions if index % 2 else conditions[::-1]
            for position, (condition_root, condition, argv) in enumerate(order):
                attempt = condition["runs"][index]
                if control is not None:
                    attempt["order"] = position
                _attempt(condition_root, index, attempt, task, argv, timeout, show_output)
    finally:
        if control_manifest is not None:
            write_once(root, CONTROL_CAPTURE, control_manifest)
            manifest["control"] = {
                "capture": CONTROL_CAPTURE,
                "manifest_hash": fingerprint(control_manifest),
            }
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
        require(
            isinstance(row, dict) and type(row.get("repetition")) is int, "Invalid capture attempt"
        )
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


def _completed(manifest, traces):
    """Traces of attempts that completed, for comparisons between repetitions."""
    done = {
        (row["task_id"], row["repetition"])
        for row in manifest["runs"]
        if row["status"] == "completed"
    }
    return {key: trace for key, trace in traces.items() if key in done}


def _quality(task, baseline, candidate, verification=None):
    fixtures = [
        {
            "id": task["id"],
            "expected": task["expected"],
            "scorer": task.get("scorer", {"type": "exact_match"}),
        }
    ]
    if verification is not None:
        fixtures.append(
            {
                "id": f"{task['id']}:local-verifier",
                "expected": True,
                "baseline_output": verification["baseline"]["passed"],
                "candidate_output": verification["candidate"]["passed"],
            }
        )
    return build_quality_report(
        fixtures,
        baseline_trace=baseline,
        candidate_trace=candidate,
        min_score=1.0,
    )


def start(
    workload_path,
    baseline_path,
    root,
    *,
    focus=None,
    objective="latency",
    minimum_improvement=0,
    decision="per_pair",
    verifier=None,
    verifier_timeout=DEFAULT_TIMEOUT,
):
    workload = read_workload(workload_path)
    manifest, traces = read_capture(baseline_path, workload)
    verifier = LocalVerifier.read(verifier, verifier_timeout) if verifier is not None else None
    require(objective in {"latency", "cost"}, "Objective must be latency or cost")
    decision = decision.replace("-", "_") if isinstance(decision, str) else decision
    require(decision in DECISIONS, "Decision must be per-pair or aggregate")
    require(
        type(minimum_improvement) in (int, float)
        and math.isfinite(minimum_improvement)
        and minimum_improvement >= 0,
        "Minimum improvement must be finite and nonnegative",
    )
    diagnoses = add_repetition_findings(
        {key: build_diagnosis(trace) for key, trace in traces.items()},
        _completed(manifest, traces),
    )
    findings = [finding for diagnosis in diagnoses.values() for finding in diagnosis["findings"]]
    if focus is None and findings:
        focus = findings[0]["type"]
    require(
        focus is None or any(item["type"] == focus for item in findings),
        "The selected finding type is absent from the baseline. Available types: "
        + ", ".join(sorted({item["type"] for item in findings})),
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
                "capture_diagnostic": row.get("diagnostic"),
                "baseline_quality": _quality(task, trace, trace)["cases"][0]["baseline"]
                if trace
                else None,
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
        if verifier is not None:
            cases[-1]["baseline_verifier"] = verifier.check(
                task,
                repetition,
                trace,
                usable=row["status"] == "completed" and trace is not None and _trace_usable(trace),
            )
    specification = {
        "schema_version": VERSION,
        "frozen_at": now(),
        "workload": workload,
        "focus": focus,
        "objective": objective,
        "decision": decision,
        "gates": asdict(gates),
        "cases": cases,
        "baseline_capture": manifest,
    }
    if verifier is not None:
        specification["verifier"] = verifier.declaration()
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


def _attempt_timing(row, trace):
    """Describe the trace boundary and the capture process boundary independently."""
    timing = {
        "trace_elapsed_ms": trace.elapsed_ms if trace is not None else None,
        "runner_elapsed_ms": None,
        "runner_clock": None,
    }
    if row.get("status") in {"runner_error", "not_started"}:
        return timing
    if "runner_elapsed_ms" in row:
        try:
            elapsed = validate_elapsed_ms(row["runner_elapsed_ms"])
        except TraceValidationError:
            return timing
        clock = "monotonic"
    else:
        # Capture 1.0 manifests predating runner timing retain UTC boundaries.
        try:
            started = datetime.fromisoformat(row.get("started_at"))
            ended = datetime.fromisoformat(row.get("ended_at"))
            if started.utcoffset() is None or ended.utcoffset() is None:
                return timing
            elapsed = (ended - started).total_seconds() * 1000
        except (TypeError, ValueError, OverflowError):
            return timing
        clock = "capture_timestamps"
    if elapsed is not None and elapsed >= 0:
        timing.update(runner_elapsed_ms=elapsed, runner_clock=clock)
    return timing


def _median_duration(values):
    if not values:
        return None
    ordered = sorted(values)
    low, high = ordered[(len(ordered) - 1) // 2], ordered[len(ordered) // 2]
    # Preserve the usual median and guard against overflow from adding two values.
    middle = low + high
    return middle / 2 if math.isfinite(middle) else low / 2 + high / 2


def _timing_summary(rows):
    eligible = [row["timing"] for row in rows if row["timing"]["eligible"]]
    summary = {"eligible_pairs": len(eligible)}
    for metric in ("trace_elapsed_ms", "runner_elapsed_ms"):
        pairs = [
            (timing["baseline"][metric], timing["candidate"][metric])
            for timing in eligible
            if all(timing[side][metric] is not None for side in ("baseline", "candidate"))
        ]
        summary[metric] = {
            "paired_count": len(pairs),
            "baseline_median_ms": _median_duration([before for before, _ in pairs]),
            "candidate_median_ms": _median_duration([after for _, after in pairs]),
            "median_paired_delta_ms": _median_duration([after - before for before, after in pairs]),
        }
    return summary


def _sign_test_p_value(improved, regressed):
    """One-sided exact sign test: chance of at least this many improvements."""
    trials = improved + regressed
    if trials == 0:
        return None
    return sum(math.comb(trials, k) for k in range(improved, trials + 1)) / 2**trials


def _aggregate_decision(rows, objective, gates):
    """Judge speed or cost across all planned pairs.

    Correctness, completion and non-performance gates were already enforced on
    every pair. Here the median paired improvement in the selected objective
    must be positive and meet the frozen minimum, and more pairs must improve
    than regress. The other metric's median regression must stay within its
    allowed regression when every pair can measure it.
    """
    secondary = "cost" if objective == "latency" else "latency"
    required = (
        gates.min_latency_improvement_pct
        if objective == "latency"
        else gates.min_cost_improvement_pct
    )
    allowed = (
        gates.max_cost_regression_pct if secondary == "cost" else gates.max_latency_regression_pct
    )
    gains = [row.get("objective_gain_pct") for row in rows]
    regressions = [
        (row["replay"] or {}).get("deltas", {}).get(f"{secondary}_regression_pct")
        if row.get("replay")
        else None
        for row in rows
    ]
    reasons = []
    measured = [gain for gain in gains if gain is not None]
    improved = sum(gain > 0 for gain in measured)
    regressed = sum(gain < 0 for gain in measured)
    median = statistics.median(measured) if measured else None
    if len(measured) != len(rows):
        reasons.append(
            f"{len(rows) - len(measured)} of {len(rows)} pairs have no measured {objective} improvement"
        )
    if median is None or median <= 0 or median < required:
        detail = "unavailable" if median is None else f"{median:.2f}%"
        reasons.append(
            f"Median {objective} improvement {detail} must be positive and at least {required:.2f}%"
        )
    if improved <= regressed:
        reasons.append(
            f"{improved} pairs improved and {regressed} regressed; more pairs must improve than regress"
        )
    secondary_median = None
    if all(value is not None for value in regressions):
        secondary_median = statistics.median(regressions)
        if secondary_median > allowed:
            reasons.append(
                f"Median {secondary} regression {secondary_median:.2f}% exceeds {allowed:.2f}% allowed"
            )
    return {
        "mode": "aggregate",
        "objective": objective,
        "pairs": len(rows),
        "median_improvement_pct": median,
        "required_improvement_pct": required,
        "improved_pairs": improved,
        "regressed_pairs": regressed,
        "unchanged_pairs": len(measured) - improved - regressed,
        "sign_test_p_value": _sign_test_p_value(improved, regressed),
        "secondary_metric": secondary,
        "secondary_median_regression_pct": secondary_median,
        "secondary_allowed_regression_pct": allowed,
        "secondary_judged": secondary_median is not None,
        "passed": not reasons,
        "reasons": reasons,
    }


def finish(session_path, candidate_path, root, *, change, verifier=None):
    session_path = Path(session_path).resolve()
    saved = load_json(session_path)
    specification = saved["specification"]
    require(saved["session_hash"] == fingerprint(specification), "Frozen session was modified")
    require(
        isinstance(change, str) and bool(change.strip()), "Describe the developer-applied change"
    )
    require(specification.get("schema_version") == VERSION, "Unsupported frozen session version")
    workload = validate_workload(specification["workload"])
    verifier = resume_verifier(specification.get("verifier"), verifier)
    require(specification.get("objective") in {"latency", "cost"}, "Unsupported frozen objective")
    # Sessions frozen before 0.2.3 have no decision field and keep per-pair judgment.
    decision = specification.get("decision", "per_pair")
    require(decision in DECISIONS, "Unsupported frozen decision")
    planned = {(task["id"], repetition) for task, repetition in slots(workload)}
    cases = specification.get("cases")
    require(
        isinstance(cases, list) and len(cases) == len(planned),
        "Frozen session must retain every planned attempt",
    )
    identities = []
    for case in cases:
        require(
            isinstance(case, dict) and type(case.get("repetition")) is int, "Invalid frozen attempt"
        )
        identities.append((case["task_id"], case["repetition"]))
    require(
        set(identities) == planned and len(set(identities)) == len(identities),
        "Duplicate or unknown frozen attempt",
    )
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
            require(trace.run_id not in baseline_ids, "Reused baseline run identity")
            require(
                fingerprint(trace.to_dict()) == case["trace_hash"], "Frozen baseline trace changed"
            )
            baseline_ids.add(trace.run_id)
            baselines[key] = trace
    candidate_ids = {trace.run_id for trace in candidates.values()}
    require(
        not baseline_ids.intersection(candidate_ids),
        "Baseline and candidate must have distinct run IDs",
    )
    controls = control_manifest = None
    if "control" in candidate_manifest:
        reference = candidate_manifest["control"]
        require(
            isinstance(reference, dict) and reference.get("capture") == CONTROL_CAPTURE,
            "Invalid interleaved control reference",
        )
        control_manifest, controls = read_capture(
            Path(candidate_path).resolve().parent / CONTROL_CAPTURE, workload
        )
        require(
            fingerprint(control_manifest) == reference.get("manifest_hash"),
            "Interleaved control capture changed",
        )
        require(
            datetime.fromisoformat(control_manifest["created_at"])
            >= datetime.fromisoformat(specification["frozen_at"]),
            "Capture the interleaved control after freezing baseline findings",
        )
        require(
            not (baseline_ids | candidate_ids).intersection(
                trace.run_id for trace in controls.values()
            ),
            "Control, baseline and candidate must have distinct run IDs",
        )
        # Diagnose controls like the original baseline, including comparisons
        # between their repetitions, so focus findings can be linked.
        control_diagnoses = add_repetition_findings(
            {key: build_diagnosis(trace) for key, trace in controls.items()},
            _completed(control_manifest, controls),
        )
    gates = ReplayGates(**specification["gates"])
    comparison_manifest = (
        specification["baseline_capture"] if controls is None else control_manifest
    )
    baseline_rows = {
        (row["task_id"], row["repetition"]): row for row in comparison_manifest["runs"]
    }
    candidate_rows = {
        (row["task_id"], row["repetition"]): row for row in candidate_manifest["runs"]
    }
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    rows, study_paths = [], {"baseline": [], "candidate": []}
    for index, case in enumerate(specification["cases"]):
        key = (case["task_id"], case["repetition"])
        task = next(task for task in workload["tasks"] if task["id"] == key[0])
        candidate = candidates.get(key)
        baseline_row, candidate_row = baseline_rows.get(key, {}), candidate_rows[key]
        if controls is None:
            label, baseline = "Baseline", baselines.get(key)
            baseline_status = case["capture_status"]
            baseline_diagnostic = case.get("capture_diagnostic")
            diagnosis, finding_ids = case["diagnosis"], case["finding_ids"]
        else:
            # Findings stay frozen from the original baseline. Measurements compare
            # the candidate with the unchanged control run alongside it.
            label, baseline = "Control", controls.get(key)
            baseline_status = baseline_row["status"]
            baseline_diagnostic = baseline_row.get("diagnostic")
            diagnosis = control_diagnoses.get(key)
            finding_ids = (
                [
                    finding["finding_id"]
                    for finding in diagnosis["findings"]
                    if finding["type"] == specification["focus"]
                ]
                if diagnosis and case["finding_ids"]
                else []
            )
        row = {
            "task_id": key[0],
            "repetition": key[1],
            "baseline_status": baseline_status,
            "candidate_status": candidate_row["status"],
            "passed": False,
            "reasons": [],
            "quality": None,
            "replay": None,
            "intervention": None,
            "timing": {
                "baseline": _attempt_timing(baseline_row, baseline),
                "candidate": _attempt_timing(candidate_row, candidate),
                "eligible": False,
            },
        }
        if controls is not None:
            row.update(
                frozen_baseline_status=case["capture_status"],
                control_ran_first=baseline_row.get("order") == 0,
            )
        verification = None
        if verifier is not None:
            verification = {
                side: verifier.check(
                    task,
                    key[1],
                    trace,
                    usable=(
                        trace is not None
                        and row[f"{side}_status"] == "completed"
                        and _trace_usable(trace)
                    ),
                )
                for side, trace in (("baseline", baseline), ("candidate", candidate))
            }
            row["verifier"] = verification
            for side, result in verification.items():
                if not result["passed"]:
                    side_label = label if side == "baseline" else "Candidate"
                    row["reasons"].append(
                        f"{side_label} local verification failed: {result['detail']}"
                    )
        if baseline is None or candidate is None:
            row["reasons"].append(f"A planned {label.lower()} or candidate trace is unavailable")
            for side, status, diagnostic in (
                (label, baseline_status, baseline_diagnostic),
                ("Candidate", candidate_row["status"], candidate_row.get("diagnostic")),
            ):
                if status != "completed":
                    row["reasons"].append(
                        f"{side} attempt is {status}. {diagnostic or 'Recapture this task with --show-runner-output to inspect the failure.'}"
                    )
        else:
            quality = _quality(task, baseline, candidate, verification)
            replay = build_replay_report(baseline, candidate, gates=gates, quality_report=quality)
            completed = baseline_status == candidate_row["status"] == "completed" and all(
                _trace_usable(trace) for trace in (baseline, candidate)
            )
            quality_passed = quality["passed"] and all(
                result[side]["passed"]
                for result in quality["cases"]
                for side in ("baseline", "candidate")
            )
            row["timing"]["eligible"] = completed and quality_passed
            if not completed:
                row["reasons"].append("An attempt failed, was interrupted or did not complete")
            if not quality_passed:
                row["reasons"].append(
                    f"{label} and candidate must both pass the declared quality checks"
                )
                fixture_result = quality["cases"][0]
                for side in ("baseline", "candidate"):
                    if not fixture_result[side]["passed"]:
                        row["reasons"].append(
                            f"{label if side == 'baseline' else 'Candidate'} answer failed the declared {task.get('scorer', {}).get('type', 'exact_match')} criterion: {fixture_result[side]['detail']}"
                        )
            # Aggregate decisions judge speed and cost across all pairs after
            # this loop; every other gate still applies to each pair.
            judged = [
                gate
                for gate in replay["gates"]["results"]
                if not gate["passed"]
                and (decision == "per_pair" or gate["name"] not in PERFORMANCE_GATES)
            ]
            if judged:
                row["reasons"].append("Native replay gates failed")
                row["reasons"].extend(f"{gate['name']}: {gate['detail']}" for gate in judged)
            if not diagnosis.get("analysis_complete", True) or not candidate.report().get(
                "analysis_complete", True
            ):
                row["reasons"].append("Finding analysis is incomplete")
            gain = replay["deltas"][
                "latency_improvement_pct"
                if specification["objective"] == "latency"
                else "cost_improvement_pct"
            ]
            if decision == "per_pair" and (gain is None or gain <= 0):
                row["reasons"].append("The selected objective has no measured improvement")
            row.update(
                quality=quality, replay=replay, passed=not row["reasons"], objective_gain_pct=gain
            )
            replay["gates"]["results"].append(
                {
                    "name": "guided_evidence",
                    "passed": row["passed"],
                    "detail": (
                        "All guided execution, quality and measured-objective checks passed"
                        if decision == "per_pair"
                        else "All guided execution and quality checks passed; performance is judged across all pairs"
                    )
                    if row["passed"]
                    else " ".join(row["reasons"]),
                    "indeterminate": False,
                }
            )
            replay["gates"]["passed"] = all(gate["passed"] for gate in replay["gates"]["results"])
            replay["summary"] = replay_summary(
                replay["deltas"],
                replay["gates"]["passed"],
                cost_evaluable=replay["gates"]["cost_evaluable"],
                pricing_known=replay["gates"]["pricing_known"],
            )
            if finding_ids:
                record = build_intervention(
                    baseline,
                    candidate,
                    target_finding_ids=finding_ids,
                    intervention_type=specification["focus"],
                    configuration={"change": change},
                    diagnosis=diagnosis,
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
                    quality_cases = row["quality"]["cases"]
                    viewed["metadata"].update(
                        quality_score=row["quality"][f"{side}_score"],
                        success=all(result[side]["passed"] for result in quality_cases)
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
    verdict = (
        {"mode": "per_pair", "passed": True}
        if decision == "per_pair"
        else _aggregate_decision(rows, specification["objective"], gates)
    )
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
        "comparison_baseline": "frozen_baseline" if controls is None else "interleaved_control",
        "control_capture_hash": None if controls is None else fingerprint(control_manifest),
        "control_runner_hash": None if controls is None else control_manifest.get("runner_hash"),
        "planned_attempts": len(rows),
        "passed_attempts": sum(row["passed"] for row in rows),
        "decision": verdict,
        "passed": all(row["passed"] for row in rows) and verdict["passed"],
        "synthetic": any(
            trace.metadata.get("synthetic") is True
            for trace in [
                *baselines.values(),
                *candidates.values(),
                *(controls or {}).values(),
            ]
        ),
        "cases": rows,
        "timing_summary": _timing_summary(rows),
        "study": study,
    }
    if verifier is not None:
        report["verifier"] = verifier.declaration()
    from agentloop.improvement_report import export_report

    export_report(root, report)
    return report
