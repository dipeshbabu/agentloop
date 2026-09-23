"""Explicit offline candidate comparisons and native paired-study artifacts."""

from __future__ import annotations

import inspect
import json
import platform
import random
from time import perf_counter

from agentloop.events import utc_now_iso
from agentloop.judgment_types import JudgeUsage, canonical, fingerprint
from agentloop.quality import build_quality_report
from agentloop.substitution_evidence import attach_trial_evidence, read_trial_evidence
from agentloop.substitution_types import (
    SUBSTITUTION_VERSION,
    DecisionImplementation,
    DecisionRequest,
    DecisionResult,
    SubstitutionProtocol,
)
from agentloop.tracer import AgentTrace, bind_trace_context
from agentloop.workflows import StageInfo, WorkflowInfo, record_operation, workflow_metadata


def _copy(value):
    return json.loads(canonical(value))


def _trace(
    protocol, example, repeat, declaration, plan_hash, outcome, started, ended, latency, output_hash
):
    failed = outcome in {"failed", "timed_out", "invalid_result"}
    status = (
        "failed" if failed else ("unknown" if outcome in {"disabled", "missing"} else "completed")
    )
    metadata = workflow_metadata(
        WorkflowInfo("offline-model-substitution", protocol.version),
        task_id=example.example_id,
        status=status,
        metadata={
            "synthetic": protocol.synthetic,
            "source": "agentloop_substitution",
            "plan_hash": plan_hash,
            "example_id": example.example_id,
            "repetition": repeat,
            "condition": declaration["name"],
        },
    )
    trace = AgentTrace(
        "Decision trial: " + declaration["name"],
        metadata=metadata,
        started_at=started,
        ended_at=ended,
        elapsed_ms=latency or 0,
    )
    if latency is not None:
        record_operation(
            protocol.step.step_id,
            kind="classifier",
            duration_ms=latency,
            started_at=started,
            ended_at=ended,
            stage=StageInfo(
                protocol.step.step_id,
                declaration["identity"]["version"],
                kind=declaration["kind"],
                input_ref=example.input_ref,
            ),
            outcome=outcome,
            output_ref="sha256:" + output_hash if output_hash is not None else None,
            status="error" if failed else "ok",
            metadata={"error_type": outcome} if failed else None,
            trace=trace,
            event_id="decision",
        )
    return trace


def _execute(protocol, example, repeat, implementation, declaration, plan_hash, enabled):
    started = utc_now_iso()
    status, usage, cost_ref, output, output_hash = (
        "disabled",
        JudgeUsage(0, 0, 0, "not_dispatched", "not_dispatched"),
        None,
        None,
        None,
    )
    latency = None
    if enabled:
        usage = JudgeUsage()
        request = DecisionRequest(
            example.example_id, example.input_ref, protocol.step, example._input_json
        )
        request.inputs  # Decode the isolated input before measuring the callback.
        started = utc_now_iso()
        began = perf_counter()
        try:
            # Experimental callbacks cannot append work to an ambient live run.
            with bind_trace_context(None):
                value = implementation.invoke(request, timeout_s=protocol.timeout_s)
            latency = (perf_counter() - began) * 1000
            ended = utc_now_iso()
            if type(value) is not DecisionResult:
                if inspect.iscoroutine(value) or inspect.isgenerator(value):
                    value.close()
                status = "invalid_result"
            else:
                status, usage, cost_ref = value.status, value.usage, value.cost_ref
                if status == "completed":
                    output = value.output
                    output_hash = fingerprint(output)
            if implementation.declaration() != declaration:
                status, output, output_hash = "invalid_result", None, None
        except TimeoutError:
            status = "timed_out"
        except Exception:
            status = "failed"
        if latency is None:
            latency = (perf_counter() - began) * 1000
            ended = utc_now_iso()
        if protocol.timeout_s is not None and latency > protocol.timeout_s * 1000:
            status, output, output_hash = "timed_out", None, None
    else:
        ended = started
    trace = _trace(
        protocol,
        example,
        repeat,
        declaration,
        plan_hash,
        status,
        started,
        ended,
        latency,
        output_hash,
    )
    attach_trial_evidence(
        trace,
        plan_hash=plan_hash,
        example_id=example.example_id,
        repetition=repeat,
        implementation=declaration,
        status=status,
        latency_ms=latency,
        usage=usage,
        cost_ref=cost_ref,
        input_hash=fingerprint(example.inputs),
        output_hash=output_hash,
    )
    return trace, output


def run_substitution_experiment(
    protocol, baseline, candidates, *, enabled=False, max_invocations=10000
):
    """Run declared implementations, then freeze quality assessments for export.

    Both baseline and candidates execute when enabled. Reference outputs and
    scorer configuration are never passed to an implementation. The host owns
    callback/scorer side effects, budgets and cooperative cancellation.
    """
    if (
        type(protocol) is not SubstitutionProtocol
        or type(baseline) is not DecisionImplementation
        or type(enabled) is not bool
    ):
        raise ValueError("experiment requires a typed protocol, baseline and boolean enabled")
    if (
        not isinstance(candidates, (tuple, list))
        or not candidates
        or any(type(item) is not DecisionImplementation for item in candidates)
    ):
        raise ValueError("at least one explicit candidate is required")
    implementations = [baseline, *candidates]
    by_name = {item.name: item for item in implementations}
    if len(by_name) != len(implementations):
        raise ValueError("baseline and candidate names must be unique")
    count = len(implementations) * len(protocol.examples) * protocol.repetitions
    if type(max_invocations) is not int or max_invocations < 1 or count > max_invocations:
        raise ValueError("planned experiment exceeds max_invocations")
    declarations = {item.name: item.declaration() for item in implementations}
    plan = {
        "protocol": protocol.declaration(),
        "baseline": baseline.name,
        "implementations": declarations,
    }
    plan_hash = fingerprint(plan)
    jobs = [
        (name, index, repeat)
        for name in sorted(by_name)
        for index in range(len(protocol.examples))
        for repeat in range(protocol.repetitions)
    ]
    random.Random(protocol.seed).shuffle(jobs)  # nosec B311
    rows, completed = [], {}
    for ordinal, (name, index, repeat) in enumerate(jobs):
        example = protocol.examples[index]
        trace, output = _execute(
            protocol, example, repeat, by_name[name], declarations[name], plan_hash, enabled
        )
        rows.append(
            {
                "ordinal": ordinal,
                "condition": name,
                "example_id": example.example_id,
                "repetition": repeat,
                "trace": trace.to_dict(),
            }
        )
        completed[name, example.example_id, repeat] = trace, output
    pairs = []
    for candidate in candidates:
        for example in protocol.examples:
            for repeat in range(protocol.repetitions):
                before, old_output = completed[baseline.name, example.example_id, repeat]
                after, new_output = completed[candidate.name, example.example_id, repeat]
                before_receipt, after_receipt = (
                    read_trial_evidence(before),
                    read_trial_evidence(after),
                )
                fixture = example.fixture(protocol.scorer)
                if before_receipt["outcome"] == "completed":
                    fixture["baseline_output"] = old_output
                if after_receipt["outcome"] == "completed":
                    fixture["candidate_output"] = new_output
                quality = None
                quality_error = None
                try:
                    quality = build_quality_report(
                        [fixture],
                        baseline_trace=before,
                        candidate_trace=after,
                        min_score=protocol.min_quality,
                    )
                except Exception:
                    quality_error = "assessment_failed"
                agreement = None
                if before_receipt["outcome"] == after_receipt["outcome"] == "completed":
                    agreement = canonical(old_output) == canonical(new_output)
                pairs.append(
                    {
                        "condition": candidate.name,
                        "example_id": example.example_id,
                        "repetition": repeat,
                        "baseline_run_id": before.run_id,
                        "candidate_run_id": after.run_id,
                        "quality": quality,
                        "quality_error": quality_error,
                        "baseline_agreement": agreement,
                    }
                )
    return _copy(
        {
            "schema_version": SUBSTITUTION_VERSION,
            **plan,
            "plan_hash": plan_hash,
            "enabled": enabled,
            "records": rows,
            "pairs": pairs,
            "environment": {
                "python": platform.python_version(),
                "system": platform.system(),
                "architecture": platform.machine(),
            },
        }
    )
