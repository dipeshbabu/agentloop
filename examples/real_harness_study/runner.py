"""Host-owned execution of a frozen same-agent ablation schedule."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from contextlib import nullcontext
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "real_harness_study"

from agentloop import reset_runtime
from agentloop.ablation_protocol import AblationProtocol
from agentloop.budget_types import ResourceUsage
from agentloop.events import utc_now_iso
from agentloop.findings import build_diagnosis
from agentloop.harness import Harness, HarnessConfig
from agentloop.optimizer import build_optimization_plan
from agentloop.tracer import trace_agent

from .protocol import load_plan, policy

try:
    from real_agent_study.agent import ToolAgent
    from real_agent_study.api import LocalModel
    from real_agent_study.run import fingerprint, verify_server, write_new
    from real_agent_study.scoring import grade
except ModuleNotFoundError:
    from examples.real_agent_study.agent import ToolAgent
    from examples.real_agent_study.api import LocalModel
    from examples.real_agent_study.run import fingerprint, verify_server, write_new
    from examples.real_agent_study.scoring import grade


def execute_observation(
    plan, task, slot, condition, position, root, base_url, *, model_factory=LocalModel
):
    root = Path(root)
    protocol = AblationProtocol.from_dict(plan["protocol"])
    spec = protocol.to_dict()["specification"]
    identity = (
        "obs_"
        + fingerprint([protocol.protocol_hash, task["id"], slot["repetition"], condition])[:24]
    )
    out = root / "attempts" / identity
    if out.exists():
        raise ValueError("observation slot already exists; retain earlier attempts")
    write_new(
        out / "started.json",
        {
            "observation_id": identity,
            "protocol_hash": protocol.protocol_hash,
            "task_id": task["id"],
            "condition": condition,
            "repetition": slot["repetition"],
        },
    )
    configuration = plan["configuration"]
    model = model_factory(
        base_url,
        plan["model"],
        max_tokens=configuration["max_output_tokens"],
        timeout_s=configuration["request_timeout_s"],
        seed=20260923 + int(slot["repetition"]),
    )
    setup_start = time.perf_counter()
    agent = ToolAgent(
        "sql",
        model,
        sources={},
        red_csv=(root / "sources/winequality-red.csv").read_text(),
        white_csv=(root / "sources/winequality-white.csv").read_text(),
        max_calls=configuration["max_model_calls"],
        timeout_s=configuration["task_timeout_s"],
    )
    setup_ms = (time.perf_counter() - setup_start) * 1000
    mode = spec["conditions"][condition]["mode"]
    metadata = {
        "synthetic": False,
        "evidence_kind": "real_local_model_ablation",
        "protocol_hash": protocol.protocol_hash,
        "task_id": task["id"],
        "repetition": slot["repetition"],
        "cache_condition": slot["cache_condition"],
        "condition": condition,
        "source_revision": spec["versions"]["source"],
    }
    trace, run, state, interrupted = None, None, None, None
    started_at, start = utc_now_iso(), time.perf_counter()
    try:
        context = (
            trace_agent("real-sql-ablation", metadata=metadata)
            if mode != "uninstrumented"
            else nullcontext(None)
        )
        with context as trace:
            model.trace = trace
            if mode in {"shadow", "enforce"}:
                limit = int(condition.rsplit("_", 1)[1])
                run = Harness(
                    HarnessConfig(
                        mode=mode, policies=(policy(limit),), capture_policy_configuration=True
                    )
                ).start_run()

                def usage_reader(result):
                    receipt = model.receipts[-1]
                    usage = receipt["usage"]
                    if usage is None:
                        return None
                    return ResourceUsage(
                        tokens=usage["prompt_tokens"] + usage["completion_tokens"],
                        token_provenance="provider",
                        cost_provenance="unavailable",
                        complete=True,
                    )

                model.complete = run.wrap(
                    model.complete, boundary="model", usage_reader=usage_reader
                )
            state = agent.run(task["prompt"])
    except BaseException as exc:
        interrupted = exc
        state = {
            "status": "interrupted" if not isinstance(exc, Exception) else "runner_error",
            "answer": None,
            "history": [],
            "error_category": type(exc).__name__,
        }
    elapsed_ms = (time.perf_counter() - start) * 1000
    if interrupted is not None and model.receipts:
        latest = model.receipts[-1]
        if latest["output_text"] is None and latest["status"] == "ok":
            latest.update(status="error", error_category=type(interrupted).__name__)
            if trace is not None and trace.events and trace.events[-1].event_type == "model_call":
                trace.events[-1].status = "error"
                trace.events[-1].error = type(interrupted).__name__
    hooks = list(run.results) if run is not None else []
    applied = [
        result
        for result in hooks
        if result.applied and result.action in {"deny", "stop", "escalate"}
    ]
    if applied:
        status = "stopped"
        last = applied[-1]
        reasons = [
            proposal.decision.reason_code
            for proposal in last.proposals
            if proposal.decision.action == last.action
        ]
        stop_reason = reasons[0] if reasons else "harness_control"
    else:
        status = {"completed": "completed", "timeout": "timed_out", "interrupted": "cancelled"}.get(
            state["status"], "failed"
        )
        stop_reason = None
    quality = grade(state["answer"], task, sources={}, tool_history=state["history"])
    if status != "completed":
        quality = {
            "score": 0.0,
            "passed": False,
            "detail": "task did not complete within its bounds",
        }
    if interrupted is not None:
        quality = {
            "score": None,
            "passed": False,
            "detail": "incomplete host execution; partial evidence retained",
        }
    usage_known = interrupted is None and all(
        receipt["usage"] is not None for receipt in model.receipts
    )
    tokens = (
        sum(
            receipt["usage"]["prompt_tokens"] + receipt["usage"]["completion_tokens"]
            for receipt in model.receipts
        )
        if usage_known
        else None
    )
    observation = {
        "observation_id": identity,
        "protocol_hash": protocol.protocol_hash,
        "task_id": task["id"],
        "repetition": slot["repetition"],
        "cache_condition": slot["cache_condition"],
        "condition": condition,
        "position": position,
        "started_at": started_at,
        "versions": spec["versions"],
        "reset_confirmed": True,
        "provider_seed": 20260923 + int(slot["repetition"]),
        "status": status,
        "stop_reason": stop_reason,
        "success": quality["passed"],
        "quality_score": quality["score"],
        "metrics": {
            "latency_ms": elapsed_ms,
            "tokens": tokens,
            "cost_usd": None,
            "model_calls": len(model.receipts) if interrupted is None else None,
            "tool_calls": sum(item["tool"] != "model" for item in state["history"])
            if interrupted is None
            else None,
            "retries": 0 if interrupted is None else None,
            "policy_eval_ms": sum(
                proposal.duration_ms or 0 for result in hooks for proposal in result.proposals
            ),
        },
        "token_status": "exact"
        if usage_known and model.receipts
        else "empty"
        if not model.receipts and interrupted is None
        else "unavailable",
        "cost_status": "unknown",
        "cost_basis": "unknown",
        "trace_run_id": trace.run_id if trace is not None else None,
    }
    output = {
        "answer": state["answer"],
        "tool_history": state["history"],
        "status": state["status"],
    }
    receipt = {
        "observation": observation,
        "output": output,
        "quality": quality,
        "setup_ms": setup_ms,
        "model_calls": model.receipts,
        "direct_recording_ms": model.recording_ms + agent.tool_recording_ms,
        "paid_provider_spend_usd": 0,
        "policy_evidence": run.export_evidence() if run is not None else None,
    }
    if trace is not None:
        trace.metadata.update(
            output=output,
            success=quality["passed"],
            quality_score=quality["score"],
            task_status=status,
        )
        trace.export_json(out / "trace.json")
        diagnosis = build_diagnosis(trace)
        write_new(out / "diagnosis.json", diagnosis)
        write_new(out / "plan.json", build_optimization_plan(trace))
        receipt.update(
            trace_hash=fingerprint(trace.to_dict()), diagnosis_hash=fingerprint(diagnosis)
        )
    write_new(out / "receipt.json", receipt)
    write_new(out / "observation.json", observation)
    if interrupted is not None:
        raise interrupted
    return receipt


def run_schedule(plan_path, base_url, model_file):
    plan_path = Path(plan_path)
    plan = load_plan(plan_path)
    root = plan_path.parent
    if (root / "execution.json").exists():
        raise ValueError("schedule already started; partial observations must not be overwritten")
    server = verify_server(base_url, plan["model"], model_file)
    write_new(
        root / "execution.json",
        {
            "protocol_hash": plan["protocol"]["protocol_hash"],
            "server": server,
            "started_at": utc_now_iso(),
        },
    )
    reset_runtime()
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    tasks = {task["id"]: task for task in plan["tasks"]}
    for slot in plan["protocol"]["specification"]["schedule"]:
        for position, condition in enumerate(slot["order"]):
            receipt = execute_observation(
                plan, tasks[slot["task_id"]], slot, condition, position, root, base_url
            )
            row = receipt["observation"]
            print(
                json.dumps(
                    {
                        "task": row["task_id"],
                        "repeat": row["repetition"],
                        "condition": condition,
                        "status": row["status"],
                        "quality": row["quality_score"],
                        "model_calls": row["metrics"]["model_calls"],
                        "latency_s": round(row["metrics"]["latency_ms"] / 1000, 3),
                    }
                ),
                flush=True,
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8766")
    parser.add_argument("--model-file", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        parser.error("real local inference requires --execute")
    run_schedule(args.plan, args.base_url, args.model_file)
