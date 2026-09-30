"""Execute actual read-only queries with frozen outputs and no artificial delay."""

from __future__ import annotations

import argparse
import json
import time
from contextlib import nullcontext
from pathlib import Path
from threading import Event, Lock
from types import MappingProxyType

from agentloop import reset_runtime
from agentloop.events import utc_now_iso
from agentloop.harness import Harness, HarnessConfig
from agentloop.scheduling_types import ToolCall, ToolContext, ToolOutcome
from agentloop.tool_scheduling import ToolScheduler
from agentloop.tracer import trace_agent
from agentloop.workflows import record_operation
from examples.real_agent_study.run import fingerprint, write_new

from .protocol import configuration, load_plan, policies, read_query


def grade(results, task):
    passed = fingerprint(results) == fingerprint(task["expected"])
    return {"passed": passed, "score": float(passed)}


def execute(plan, task, slot, condition, position, root, *, query_runner=read_query):
    root = Path(root)
    spec = plan["protocol"]["specification"]
    identity = (
        "obs_"
        + fingerprint(
            [plan["protocol"]["protocol_hash"], task["id"], slot["repetition"], condition]
        )[:24]
    )
    out = root / "attempts" / identity
    write_new(out / "started.json", {"started_at": utc_now_iso(), "condition": condition})
    metadata = {
        "synthetic": False,
        "task_id": task["id"],
        "repetition": slot["repetition"],
        "cache_condition": slot["cache_condition"],
        "condition": condition,
        "protocol_hash": plan["protocol"]["protocol_hash"],
    }
    context = (
        trace_agent("read-only-tool-scheduling", metadata=metadata)
        if condition != "off"
        else nullcontext(None)
    )
    receipts, lock = [], Lock()
    scheduler, run, trace, result, failure, outcomes = None, None, None, None, None, []
    status, error = "completed", None
    started_at, start = utc_now_iso(), time.perf_counter()
    try:
        with context as trace:

            def tool(query):
                def invoke(context):
                    began, tool_start = utc_now_iso(), time.perf_counter()
                    output, caught = None, None
                    try:
                        output = query_runner(root / "wine.sqlite", query)
                        return {"call_id": query["id"], "rows": output}
                    except BaseException as exc:
                        caught = exc
                        raise
                    finally:
                        duration = (time.perf_counter() - tool_start) * 1000
                        ended = utc_now_iso()
                        with lock:
                            receipts.append(
                                {
                                    "call_id": query["id"],
                                    "output": output,
                                    "status": "failed" if caught else "completed",
                                    "duration_ms": duration,
                                    "started_at": began,
                                    "ended_at": ended,
                                }
                            )
                        if trace is not None:
                            record_operation(
                                "read_query",
                                kind="tool",
                                trace=trace,
                                event_id=query["id"],
                                duration_ms=duration,
                                started_at=began,
                                ended_at=ended,
                                depends_on=[],
                                metadata={
                                    "parallel_safe": True,
                                    "resource_reads": ["wine-dataset"],
                                    "resource_writes": [],
                                    "read_only": True,
                                },
                                status="error" if caught else "ok",
                                error="query_failed" if caught else None,
                            )

                return ToolCall(
                    query["id"],
                    invoke,
                    depends_on=(),
                    reads=("wine-dataset",),
                    writes=(),
                    concurrent=True,
                    effect="read_only",
                )

            calls = [tool(query) for query in task["calls"]]
            if condition in {"off", "trace"}:
                for call in calls:
                    value = call.invoke(
                        ToolContext(call.call_id, MappingProxyType({}), Event(), None)
                    )
                    outcomes.append(ToolOutcome(call.call_id, "completed", value))
            else:
                run = Harness(
                    HarnessConfig(
                        mode=spec["conditions"][condition]["mode"],
                        policies=tuple(
                            policy
                            for policy in policies()
                            if policy.policy_id in spec["conditions"][condition]["policies"]
                        ),
                    )
                ).start_run()
                if condition.endswith("budget"):
                    for call in calls:
                        protected = run.wrap(
                            call.invoke,
                            boundary="tool",
                            branch_id=call.call_id,
                            dispatch=call.dispatch,
                        )
                        value = protected(
                            ToolContext(call.call_id, MappingProxyType({}), Event(), None)
                        )
                        outcomes.append(ToolOutcome(call.call_id, "completed", value))
                else:
                    scheduler = ToolScheduler(run, config=configuration())
                    result = scheduler.execute(calls)
                    outcomes = list(result.outcomes)
                    if not result.completed:
                        status = "failed"
    except BaseException as exc:
        failure, error = exc, type(exc).__name__
        status = "cancelled" if not isinstance(exc, Exception) else "failed"
        if scheduler is not None and scheduler.last_result is not None:
            outcomes = list(scheduler.last_result.outcomes)
        elif receipts:
            latest = receipts[-1]
            if latest["status"] == "completed":
                outcomes.append(
                    ToolOutcome(
                        latest["call_id"],
                        "completed",
                        {"call_id": latest["call_id"], "rows": latest["output"]},
                        exc,
                    )
                )
            else:
                outcomes.append(
                    ToolOutcome(
                        latest["call_id"],
                        "cancelled" if status == "cancelled" else "failed",
                        error=exc,
                    )
                )
    by_id = {outcome.call_id: outcome for outcome in outcomes}
    outcomes = [
        by_id.get(query["id"], ToolOutcome(query["id"], "not_started")) for query in task["calls"]
    ]
    elapsed = (time.perf_counter() - start) * 1000
    values = [outcome.value for outcome in outcomes if outcome.status == "completed"]
    quality = None if status == "cancelled" else grade(values, task)
    success = quality["passed"] and status == "completed" if quality else None
    score = quality["score"] if quality and success else 0.0 if quality else None
    hooks = () if run is None else run.results
    row = {
        "observation_id": identity,
        "protocol_hash": plan["protocol"]["protocol_hash"],
        "task_id": task["id"],
        "repetition": slot["repetition"],
        "cache_condition": slot["cache_condition"],
        "condition": condition,
        "position": position,
        "started_at": started_at,
        "versions": spec["versions"],
        "reset_confirmed": True,
        "provider_seed": None,
        "status": status,
        "stop_reason": None,
        "success": success,
        "quality_score": score,
        "metrics": {
            "latency_ms": elapsed,
            "tokens": 0,
            "cost_usd": None,
            "model_calls": 0,
            "tool_calls": len(receipts),
            "retries": 0,
            "policy_eval_ms": sum(
                proposal.duration_ms or 0 for hook in hooks for proposal in hook.proposals
            ),
        },
        "token_status": "empty",
        "cost_status": "unknown",
        "cost_basis": "unknown",
        "trace_run_id": trace.run_id if trace else None,
    }
    if status == "cancelled":
        row["metrics"] = {key: None for key in row["metrics"]}
        row["token_status"] = "unavailable"
    receipt = {
        "observation": row,
        "quality": quality,
        "results": values,
        "outcomes": [
            {"call_id": outcome.call_id, "status": outcome.status} for outcome in outcomes
        ],
        "tool_receipts": receipts,
        "error_category": error,
        "schedule_evidence": scheduler.export_evidence() if scheduler else None,
        "policy_evidence": run.export_evidence() if run else None,
        "paid_provider_spend_usd": 0,
    }
    if trace is not None:
        trace.metadata.update(success=success, quality_score=score, task_status=status)
        trace.export_json(out / "trace.json")
        receipt["trace_hash"] = fingerprint(trace.to_dict())
    write_new(out / "receipt.json", receipt)
    if failure is not None and not isinstance(failure, Exception):
        raise failure
    return receipt


def run_schedule(plan_path):
    plan_path = Path(plan_path)
    plan = load_plan(plan_path)
    tasks = {task["id"]: task for task in plan["tasks"]}
    write_new(
        plan_path.parent / "execution.json",
        {"started_at": utc_now_iso(), "protocol_hash": plan["protocol"]["protocol_hash"]},
    )
    reset_runtime()
    for slot in plan["protocol"]["specification"]["schedule"]:
        for position, condition in enumerate(slot["order"]):
            receipt = execute(
                plan, tasks[slot["task_id"]], slot, condition, position, plan_path.parent
            )
            row = receipt["observation"]
            print(
                json.dumps(
                    {
                        key: row[key]
                        for key in ("task_id", "repetition", "condition", "status", "quality_score")
                    }
                ),
                flush=True,
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        parser.error("explicit --execute is required")
    run_schedule(args.plan)
