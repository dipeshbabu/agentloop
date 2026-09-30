"""Measure the frozen route, preserving original predictions before candidates."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from agentloop import reset_runtime
from agentloop.budgets import BudgetLimits, budget_policy
from agentloop.events import utc_now_iso
from agentloop.findings import build_diagnosis
from agentloop.harness import Harness, HarnessConfig
from agentloop.interventions import build_intervention
from agentloop.model_routing import ModelRouter, routing_policy
from agentloop.quality import build_quality_report
from agentloop.replay import ReplayGates
from agentloop.routing_types import RoutingRequest
from agentloop.tracer import AgentTrace, trace_agent
from examples.real_agent_study.run import MODELS, fingerprint, verify_server, write_new
from examples.real_calibration_study import normalize_answer

from .protocol import configuration, load_plan
from .runtime import LocalBackend, identity


def execute(plan, task, condition, repetition, root, base_url, *, client_factory=LocalBackend):
    root = Path(root)
    folder = root / "attempts" / condition / str(repetition) / task["id"]
    write_new(
        folder / "started.json",
        {
            "plan_hash": plan["plan_hash"],
            "condition": condition,
            "repetition": repetition,
            "task_id": task["id"],
            "started_at": utc_now_iso(),
        },
    )
    settings = plan["configuration"]
    payload = {
        "model": identity("baseline").model,
        "messages": [
            {"role": "system", "content": settings["prompt"]},
            {"role": "user", "content": task["prompt"]},
        ],
        "max_tokens": settings["max_tokens"],
        "temperature": settings["temperature"],
        "seed": 20260930 + repetition,
        "cache_prompt": settings["cache_prompt"],
        "response_format": settings["response_format"],
    }
    answer, status, error, failure = None, "completed", None, None
    metadata = {
        "synthetic": False,
        "evidence_kind": "real_local_model_execution",
        "workload": "explicit_route_numeric_answer",
        "task_id": task["id"],
        "repetition": repetition,
        "condition": condition,
        "protocol_hash": plan["plan_hash"],
    }
    with trace_agent("capability-checked-routing-study", metadata=metadata) as trace:
        client = client_factory(base_url, condition, trace)
        config = configuration(condition)
        run = Harness(
            HarnessConfig(
                mode="enforce",
                policies=(
                    routing_policy(config),
                    budget_policy(
                        BudgetLimits(
                            max_model_calls=settings["model_call_limit"],
                            max_tokens=settings["token_limit"],
                        )
                    ),
                ),
            )
        ).start_run()
        router = ModelRouter(
            run, config=config, backends=(client.backend("baseline"), client.backend("candidate"))
        )
        start = time.perf_counter()
        try:
            reply = router(RoutingRequest(payload))
            raw = json.loads(reply["choices"][0]["message"]["content"])
            answer = normalize_answer(raw.get("answer")) if isinstance(raw, dict) else None
            if answer is None:
                status, error = "failed", "invalid_answer"
        except BaseException as exc:
            failure, error = exc, type(exc).__name__
            status = "cancelled" if not isinstance(exc, Exception) else "failed"
        elapsed = (time.perf_counter() - start) * 1000
    score = (
        None
        if status == "cancelled"
        else float(status == "completed" and answer == task["expected"])
    )
    trace.metadata.update(
        output=answer,
        success=bool(score) if score is not None else None,
        quality_score=score,
        task_status=status,
    )
    trace.export_json(folder / "trace.json")
    receipt = {
        "plan_hash": plan["plan_hash"],
        "task_id": task["id"],
        "condition": condition,
        "repetition": repetition,
        "status": status,
        "error_category": error,
        "answer": answer,
        "quality_score": score,
        "decision_latency_ms": elapsed if status != "cancelled" else None,
        "calls": client.calls,
        "tokenization": client.tokenization,
        "routing": router.export_evidence(),
        "harness": run.export_evidence(),
        "trace_hash": fingerprint(trace.to_dict()),
        "paid_provider_spend_usd": 0,
        "operating_cost_usd": None,
    }
    write_new(folder / "receipt.json", receipt)
    diagnosis = build_diagnosis(trace)
    write_new(folder / "diagnosis.json", diagnosis)
    if condition == "baseline":
        selected = [
            item for item in diagnosis["findings"] if item["type"] == "route_to_smaller_model"
        ]
        selected = selected if len(selected) == 1 else []
        prediction = {
            "selected": selected,
            "rejected": [item for item in diagnosis["findings"] if item not in selected],
            "selection_reason": "One explicit answer-model substitution; no automatic promotion"
            if len(selected) == 1
            else "No uniquely attributable routing finding; retain unlinked outcome",
        }
        write_new(folder / "prediction.json", prediction)
        write_new(
            folder / "journal.json",
            {
                "prediction_hash": fingerprint(prediction),
                "prediction_recorded_at": utc_now_iso(),
                "plan_hash": plan["plan_hash"],
            },
        )
    else:
        before = root / "attempts/baseline" / str(repetition) / task["id"]
        baseline = AgentTrace.from_json(before / "trace.json")
        prediction = json.loads((before / "prediction.json").read_text(encoding="utf-8"))
        quality = build_quality_report(
            [
                {
                    "id": task["id"],
                    "expected": task["expected"],
                    "baseline_output": baseline.metadata.get("output"),
                    "candidate_output": answer,
                }
            ],
            min_score=1,
        )
        write_new(folder / "quality.json", quality)
        if prediction["selected"]:
            baseline_diagnosis = json.loads((before / "diagnosis.json").read_text(encoding="utf-8"))
            record = build_intervention(
                baseline,
                trace,
                target_finding_ids=[prediction["selected"][0]["finding_id"]],
                intervention_type="explicit_model_routing",
                configuration={
                    "original": MODELS["baseline"],
                    "target": MODELS["candidate"],
                    "scope": "one answer model call with capability checks",
                },
                diagnosis=baseline_diagnosis,
                gates=ReplayGates(min_quality_score=1),
                quality_report=quality,
                metadata={
                    "synthetic": False,
                    "task_id": task["id"],
                    "repetition": repetition,
                    "protocol_hash": plan["plan_hash"],
                },
            ).to_dict()
            write_new(folder / "intervention.json", record)
        write_new(
            folder / "journal.json",
            {
                "outcome_recorded_at": utc_now_iso(),
                "plan_hash": plan["plan_hash"],
                "original_prediction_hash": fingerprint(prediction),
                "linked": bool(prediction["selected"]),
            },
        )
    if failure is not None and not isinstance(failure, Exception):
        raise failure
    return receipt


def run_block(plan_path, condition, repetition, base_url, model_file):
    plan_path = Path(plan_path)
    plan = load_plan(plan_path)
    if condition not in MODELS or repetition not in range(plan["configuration"]["repetitions"]):
        raise ValueError("condition or repetition not in frozen schedule")
    reset_runtime()
    server = verify_server(base_url, MODELS[condition], model_file)
    if condition == "candidate":
        for task in plan["tasks"]:
            folder = plan_path.parent / "attempts/baseline" / str(repetition) / task["id"]
            journal = json.loads((folder / "journal.json").read_text(encoding="utf-8"))
            prediction = json.loads((folder / "prediction.json").read_text(encoding="utf-8"))
            if journal["plan_hash"] != plan["plan_hash"] or journal[
                "prediction_hash"
            ] != fingerprint(prediction):
                raise ValueError("candidate requires original archived prediction registration")
    write_new(
        plan_path.parent / "blocks" / f"{condition}-{repetition}.json",
        {
            "plan_hash": plan["plan_hash"],
            "server": server,
            "condition": condition,
            "repetition": repetition,
            "started_at": utc_now_iso(),
        },
    )
    for task in plan["tasks"]:
        receipt = execute(plan, task, condition, repetition, plan_path.parent, base_url)
        print(
            json.dumps(
                {
                    key: receipt[key]
                    for key in ("task_id", "condition", "repetition", "status", "quality_score")
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--condition", choices=tuple(MODELS), required=True)
    parser.add_argument("--repetition", type=int, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8766")
    parser.add_argument("--model-file", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        parser.error("explicit --execute is required")
    run_block(args.plan, args.condition, args.repetition, args.base_url, args.model_file)
