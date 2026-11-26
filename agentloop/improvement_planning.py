"""Plan a comparison from a capture: guidance and the frozen session.

Split out of ``agentloop.improvement`` (issue #407), which re-exports every
name, so existing imports keep working.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict
from pathlib import Path

from agentloop.experiment_artifacts import write_once
from agentloop.experiment_types import fingerprint
from agentloop.findings import build_diagnosis
from agentloop.improvement_capture import (
    DECISIONS,
    VERSION,
    now,
    read_capture,
    read_workload,
    require,
    slots,
)
from agentloop.improvement_verifier import DEFAULT_TIMEOUT, LocalVerifier
from agentloop.quality import build_quality_report
from agentloop.repetitions import add_repetition_findings
from agentloop.replay import ReplayGates
from agentloop.schema import TraceValidationError, validate_elapsed_ms
from agentloop.tokens import is_token_basis_evaluable


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


def _runner_budget(value):
    try:
        return validate_elapsed_ms(value)
    except TraceValidationError as error:
        raise ValueError(
            "Runner regression budget must be a finite nonnegative number in ms"
        ) from error


MIN_SIGN_TEST_PAIRS = 5
NOISY_SPREAD_PCT = 10.0


def planning_guidance(workload, manifest, traces, objective, minimum_improvement):
    """Warn when the planned comparison cannot separate a change from noise.

    The sign test needs at least five pairs before even a unanimous result can
    fall below p = 0.05. When repeated baseline attempts of the same task vary
    in the objective (latency, or estimated cost) by more than the required
    improvement, smaller differences may be noise. For the cost objective,
    attempts whose cost cannot be measured are reported separately.
    """
    notes = []
    pairs = len(workload["tasks"]) * workload["repetitions"]
    if pairs < MIN_SIGN_TEST_PAIRS:
        notes.append(
            {
                "kind": "few_pairs",
                "pairs": pairs,
                "message": f"Only {pairs} pair{'s' if pairs != 1 else ''} planned. Even if every "
                "pair improves, a one-sided sign test cannot fall below p = 0.05 with fewer than "
                f"{MIN_SIGN_TEST_PAIRS} pairs. Add tasks or raise repetitions in the task file.",
            }
        )
    if workload["repetitions"] < 2:
        notes.append(
            {
                "kind": "noise_unmeasured",
                "message": "Each task runs once, so baseline run-to-run variation cannot be "
                "measured. Use at least 2 repetitions to see how noisy the agent is.",
            }
        )
    else:
        completed = {
            key: trace
            for key, trace in _completed(manifest, traces).items()
            if _trace_usable(trace)
        }
        if objective == "latency":
            values = {key: trace.elapsed_ms for key, trace in completed.items()}
        else:
            values, unmeasurable = {}, 0
            for key, trace in completed.items():
                report = trace.report()
                if report.get("has_unknown_cost") or not is_token_basis_evaluable(
                    report.get("token_status")
                ):
                    unmeasurable += 1
                else:
                    values[key] = float(report.get("estimated_cost_usd") or 0.0)
            if unmeasurable:
                notes.append(
                    {
                        "kind": "cost_unmeasurable",
                        "attempts": unmeasurable,
                        "message": f"{unmeasurable} of {len(completed)} completed baseline "
                        "attempts have unknown model pricing or estimated token counts, so "
                        "their cost cannot be compared and cost gates will be indeterminate. "
                        "Record provider token usage and use models with known pricing.",
                    }
                )
        spreads = []
        for task in workload["tasks"]:
            task_values = [value for (task_id, _), value in values.items() if task_id == task["id"]]
            middle = statistics.median(task_values) if len(task_values) >= 2 else 0
            if middle > 0:
                spreads.append((max(task_values) - min(task_values)) / middle * 100)
        if spreads:
            spread = statistics.median(spreads)
            if spread >= NOISY_SPREAD_PCT and spread > minimum_improvement:
                notes.append(
                    {
                        "kind": "noisy_baseline",
                        "objective": objective,
                        "spread_pct": round(spread, 2),
                        "message": f"Baseline {objective} of the same task varied by a median "
                        f"{spread:.1f}% between repetitions, more than the "
                        f"{minimum_improvement:g}% required improvement. Smaller differences "
                        "may be noise: require a larger improvement, add repetitions, or "
                        "compare against an interleaved control with the aggregate decision.",
                    }
                )
    return notes


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
    max_runner_regression_ms=None,
):
    workload = read_workload(workload_path)
    manifest, traces = read_capture(baseline_path, workload)
    verifier = LocalVerifier.read(verifier, verifier_timeout) if verifier is not None else None
    require(objective in {"latency", "cost"}, "Objective must be latency or cost")
    decision = decision.replace("-", "_") if isinstance(decision, str) else decision
    require(decision in DECISIONS, "Decision must be per-pair or aggregate")
    runner_budget = _runner_budget(max_runner_regression_ms)
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
        "planning_notes": planning_guidance(
            workload, manifest, traces, objective, minimum_improvement
        ),
    }
    if verifier is not None:
        specification["verifier"] = verifier.declaration()
    if runner_budget is not None:
        specification["max_runner_regression_ms"] = runner_budget
    write_once(
        root,
        "session.json",
        {"session_hash": fingerprint(specification), "specification": specification},
    )
    from agentloop.improvement_report import plan_markdown

    (root / "plan.md").write_text(plan_markdown(specification), encoding="utf-8")
    return specification
