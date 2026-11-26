"""Freeze a workload and capture baseline runs of it.

Split out of ``agentloop.improvement`` (issue #407), which re-exports every
name, so existing imports keep working.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import subprocess  # nosec B404 - execution requires explicit CLI argv
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter_ns

from agentloop.experiment_artifacts import local_path, read_json, write_once
from agentloop.experiment_types import fingerprint
from agentloop.quality import validate_quality_fixtures
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
