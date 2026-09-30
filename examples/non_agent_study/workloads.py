"""Three application-owned CPU inference paths with identical recorded/unrecorded logic."""

from __future__ import annotations

from time import perf_counter

from agentloop import (
    StageInfo,
    WorkflowInfo,
    record_operation,
    set_workflow_outcome,
    trace_workflow,
)
from agentloop.events import utc_now_iso
from agentloop.quality import build_quality_report
from examples.non_agent_study.data import digest


def run_task(workload, task, models, *, variant, repetition, protocol_hash, recording=True):
    if variant not in {"baseline", "batched", "cheap"}:
        raise ValueError("unsupported application configuration")
    # Gold labels and split/group information never reach the inference functions.
    rows = [
        {
            key: value
            for key, value in row.items()
            if key in {"id", "features", "left_ref", "right_ref"}
        }
        for row in task["rows"]
    ]
    name = workload["id"]
    with trace_workflow(
        name + "/" + variant,
        workflow=WorkflowInfo("nonagent." + name, "1.0"),
        task_id=task["id"],
        metadata={
            "synthetic": workload.get("synthetic", False),
            "evidence_kind": "actual_local_learned_model_execution",
            "workload": name,
            "variant": variant,
            "repetition": repetition,
            "split": task["split"],
            "protocol_hash": protocol_hash,
            "model_versions": {key: value.parameters["sha256"] for key, value in models.items()},
            "recording": recording,
            "operating_cost_usd": None,
            "paid_provider_spend_usd": 0,
        },
    ) as trace:

        def invoke(identity, inputs, event_id, dependencies=()):
            started, began = utc_now_iso(), perf_counter()
            result = models[identity].predict(inputs)
            elapsed = (perf_counter() - began) * 1000
            ended = utc_now_iso()
            if recording:
                model_hash = models[identity].parameters["sha256"]
                record_operation(
                    name + "." + identity,
                    kind="model",
                    stage=StageInfo(name + "." + identity, model_hash, kind="classifier"),
                    started_at=started,
                    ended_at=ended,
                    duration_ms=elapsed,
                    trace=trace,
                    event_id=event_id,
                    depends_on=list(dependencies),
                    model="local-numpy/" + name + "/" + identity + "/" + model_hash[:12],
                    token_provenance="unavailable",
                    metadata={
                        "provider": "local-numpy",
                        "batch_safe": True,
                        "side_effects": "none",
                        "numeric_model": True,
                        "usage_applicability": "not_applicable_non_language_model",
                        "record_count": len(inputs),
                        "input_ref": "sha256:" + digest(inputs),
                        "output_ref": "sha256:" + digest(result),
                        "model_ref": "sha256:" + model_hash,
                    },
                )
            return result

        if name != "dry-bean":
            identity = "cheap" if variant == "cheap" else "baseline"
            if variant == "batched":
                results = invoke(identity, rows, "predict-batch")
            else:
                results = [
                    invoke(identity, [row], f"predict-{index:03d}")[0]
                    for index, row in enumerate(rows)
                ]
        else:
            results = (
                invoke("primary", rows, "primary-batch")
                if variant == "batched"
                else [
                    invoke("primary", [row], f"primary-{index:03d}")[0]
                    for index, row in enumerate(rows)
                ]
            )
            uncertain, gate_ids = [], []
            for index, result in enumerate(results):
                started, began = utc_now_iso(), perf_counter()
                required = result["confidence"] < workload["fallback_margin"] and variant != "cheap"
                elapsed = (perf_counter() - began) * 1000
                gate_id = f"gate-{index:03d}"
                if recording:
                    record_operation(
                        "dry-bean.confidence_gate",
                        kind="rule",
                        stage=StageInfo("confidence-gate", "margin-v1", kind="rule"),
                        started_at=started,
                        ended_at=utc_now_iso(),
                        duration_ms=elapsed,
                        event_id=gate_id,
                        trace=trace,
                        depends_on=[
                            "primary-batch" if variant == "batched" else f"primary-{index:03d}"
                        ],
                        outcome="fallback" if required else "primary",
                        metadata={"threshold": workload["fallback_margin"], "side_effects": "none"},
                    )
                if required:
                    uncertain.append(index)
                    gate_ids.append(gate_id)
            if variant == "batched" and uncertain:
                fallback = invoke(
                    "fallback", [rows[index] for index in uncertain], "fallback-batch", gate_ids
                )
                for index, result in zip(uncertain, fallback):
                    results[index] = result
            elif variant != "cheap":
                for index in uncertain:
                    results[index] = invoke(
                        "fallback", [rows[index]], f"fallback-{index:03d}", [f"gate-{index:03d}"]
                    )[0]
        output = {row["id"]: result["label"] for row, result in zip(rows, results)}
        trace.metadata["output"] = output
        if name == "record-linkage":
            trace.metadata["matches"] = [
                [row["left_ref"], row["right_ref"]]
                for row, result in zip(rows, results)
                if result["label"] == "TRUE"
            ]
        set_workflow_outcome(trace, "decisions_recorded", output_ref="sha256:" + digest(output))
    return trace


def quality_report(workload, task, baseline, candidate):
    if workload["id"] == "record-linkage":
        expected = [
            [row["left_ref"], row["right_ref"]] for row in task["rows"] if row["label"] == "TRUE"
        ]
        fixtures = [
            {
                "schema_version": "2.0",
                "id": task["id"],
                "expected": expected,
                "baseline_output": baseline.metadata["matches"],
                "candidate_output": candidate.metadata["matches"],
                "expected_ref": "dataset:" + workload["prepared_sha256"] + ":" + task["id"],
                "scorer": {"type": "matches", "version": "1.0", "symmetric": False},
            }
        ]
    else:
        labels = sorted(
            {row["label"] for row in task["rows"]}
            | set(baseline.metadata["output"].values())
            | set(candidate.metadata["output"].values())
        )
        fixtures = [
            {
                "schema_version": "2.0",
                "id": row["id"],
                "expected": row["label"],
                "baseline_output": baseline.metadata["output"][row["id"]],
                "candidate_output": candidate.metadata["output"][row["id"]],
                "input_ref": "sha256:" + digest(row["features"]),
                "expected_ref": "dataset:" + workload["prepared_sha256"] + ":" + row["id"],
                "scorer": {"type": "decision", "version": "1.0", "labels": labels},
            }
            for row in task["rows"]
        ]
    return build_quality_report(
        fixtures,
        baseline_trace=baseline,
        candidate_trace=candidate,
        min_score=workload["quality"]["minimum_score"],
    )
