"""Validate every declared scheduling observation and reuse native reports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agentloop.ablations import ablation_to_markdown, build_ablation_report, summarize_ablation
from agentloop.findings import build_diagnosis
from agentloop.graph import ExecutionGraph
from agentloop.html_report import analysis_to_html
from agentloop.optimizer import build_optimization_plan
from agentloop.tracer import AgentTrace
from examples.real_agent_study.run import fingerprint, write_new

from .protocol import load_plan
from .runner import grade


def export(plan_path, out):
    plan_path, out = Path(plan_path), Path(out)
    plan = load_plan(plan_path)
    if out.exists() and any(out.iterdir()):
        raise ValueError("export directory must be fresh")
    spec = plan["protocol"]["specification"]
    tasks = {task["id"]: task for task in plan["tasks"]}
    slots = {}
    for slot in spec["schedule"]:
        for position, condition in enumerate(slot["order"]):
            identity = (
                "obs_"
                + fingerprint(
                    [
                        plan["protocol"]["protocol_hash"],
                        slot["task_id"],
                        slot["repetition"],
                        condition,
                    ]
                )[:24]
            )
            slots[identity] = {
                "observation_id": identity,
                "protocol_hash": plan["protocol"]["protocol_hash"],
                "task_id": slot["task_id"],
                "repetition": slot["repetition"],
                "cache_condition": slot["cache_condition"],
                "condition": condition,
                "position": position,
                "versions": spec["versions"],
            }
    attempts = plan_path.parent / "attempts"
    if attempts.exists() and any(path.name not in slots for path in attempts.iterdir()):
        raise ValueError("unplanned attempt artifact")
    observations, validated, hashes, missing = [], [], {}, []
    for identity, slot in slots.items():
        folder = attempts / identity
        path = folder / "receipt.json"
        if not path.exists():
            missing.append(
                {
                    "observation_id": identity,
                    "status": "incomplete"
                    if (folder / "started.json").exists()
                    else "not_recorded",
                }
            )
            continue
        receipt = json.loads(path.read_text(encoding="utf-8"))
        row = receipt["observation"]
        if any(row.get(key) != value for key, value in slot.items()):
            raise ValueError("observation does not match its frozen slot")
        task = tasks[row["task_id"]]
        order = [call["id"] for call in task["calls"]]
        if [item["call_id"] for item in receipt["outcomes"]] != order:
            raise ValueError("call/result association changed")
        tool_results = {item["call_id"]: item for item in receipt["tool_receipts"]}
        if len(tool_results) != len(receipt["tool_receipts"]) or not tool_results.keys() <= set(
            order
        ):
            raise ValueError("tool call identity changed")
        expected_results = [
            {"call_id": item["call_id"], "rows": tool_results[item["call_id"]]["output"]}
            for item in receipt["outcomes"]
            if item["status"] == "completed"
        ]
        if expected_results != receipt["results"]:
            raise ValueError("retained output differs from completed tool work")
        if row["status"] == "cancelled":
            if (
                row["success"] is not None
                or row["quality_score"] is not None
                or receipt["quality"] is not None
                or any(value is not None for value in row["metrics"].values())
            ):
                raise ValueError("cancelled work cannot claim complete measurements")
        else:
            quality = grade(receipt["results"], task)
            success = quality["passed"] and row["status"] == "completed"
            if (
                quality != receipt["quality"]
                or row["success"] != success
                or row["quality_score"] != (quality["score"] if success else 0.0)
            ):
                raise ValueError("independent quality changed")
            if (
                row["metrics"]["tool_calls"] != len(tool_results)
                or row["metrics"]["model_calls"] != 0
                or row["metrics"]["tokens"] != 0
                or row["metrics"]["retries"] != 0
            ):
                raise ValueError("actual workload counts changed")
        if (
            row["metrics"]["cost_usd"] is not None
            or row["cost_status"] != "unknown"
            or row["cost_basis"] != "unknown"
        ):
            raise ValueError("operating cost was not measured")
        schedule = receipt["schedule_evidence"]
        has_scheduler = slot["condition"].endswith(("schedule", "combined"))
        if bool(schedule) != has_scheduler:
            raise ValueError("scheduling evidence differs from the condition")
        if schedule:
            records = list(schedule["records"].values())
            if len(records) != 1:
                raise ValueError("one schedule is required per observation")
            record = records[0]
            if (
                record["peak_running"] > record["actual_concurrency_limit"]
                or [item["call_id"] for item in record["plan"]["calls"]] != order
            ):
                raise ValueError("schedule exceeded its declaration")
            if record["actual_concurrency_limit"] != (
                4 if slot["condition"].startswith("enforce") else 1
            ):
                raise ValueError("actual schedule mode changed")
            for item in receipt["outcomes"]:
                if record["calls"][item["call_id"]]["status"] != item["status"]:
                    raise ValueError("schedule outcome changed")
        trace = None
        if (row["trace_run_id"] is None) != (slot["condition"] == "off"):
            raise ValueError("trace presence contradicts condition")
        if row["trace_run_id"] is not None:
            trace = AgentTrace.from_json(folder / "trace.json")
            if (
                fingerprint(trace.to_dict()) != receipt["trace_hash"]
                or trace.run_id != row["trace_run_id"]
                or trace.metadata["quality_score"] != row["quality_score"]
                or trace.metadata["success"] != row["success"]
            ):
                raise ValueError("native trace changed")
            if (
                trace.metadata.get("agentloop.tool_schedule") != schedule
                or trace.metadata.get("agentloop.harness") != receipt["policy_evidence"]
            ):
                raise ValueError("native control evidence diverged from host receipt")
            if not ExecutionGraph.from_trace(trace).dependency_summary()["valid"]:
                raise ValueError("recorded dependencies became invalid")
            if {event.event_id for event in trace.events} != set(tool_results):
                raise ValueError("native tool spans diverged from actual calls")
        elif (folder / "trace.json").exists() or receipt["policy_evidence"] is not None:
            raise ValueError("uninstrumented condition cannot fabricate spans")
        observations.append(row)
        validated.append((row, trace))
        hashes[identity] = fingerprint(receipt)
    build_ablation_report(plan["protocol"], observations)
    out.mkdir(parents=True, exist_ok=True)
    manifests, html_done = {}, set()
    for row, trace in validated:
        if trace is None:
            continue
        condition = row["condition"]
        target = out / "traces" / condition / (row["observation_id"] + ".json")
        trace.export_json(target)
        manifests.setdefault(condition, []).append(target.relative_to(out).as_posix())
        if condition not in html_done:
            payload = {
                "trace": trace.to_dict(),
                "report": trace.report(),
                "diagnosis": build_diagnosis(trace),
                "optimization": build_optimization_plan(trace),
            }
            (out / f"{condition}.html").write_text(analysis_to_html(payload), encoding="utf-8")
            html_done.add(condition)
    if "trace" in manifests and len(manifests) >= 2:
        write_new(
            out / "study.json",
            {
                "schema_version": "1.0",
                "name": spec["name"],
                "baseline": "trace",
                "conditions": manifests,
                "pairing_keys": ["task_id", "repetition", "cache_condition"],
                "bootstrap": spec["bootstrap"],
            },
        )
        write_new(
            out / "bundle.json",
            {
                "schema_version": "1.0",
                "protocol": plan["protocol"],
                "observations": observations,
                "study_manifest": "study.json",
                "interventions": [],
            },
        )
        report = summarize_ablation(out / "bundle.json")
    else:
        report = build_ablation_report(plan["protocol"], observations)
        report["artifact_links_complete"] = False
        write_new(out / "observations.json", observations)
    write_new(out / "report.json", report)
    write_new(
        out / "provenance.json",
        {
            "receipt_hashes": hashes,
            "unavailable": missing,
            "ledger_scope": "Explicit dependency/resource scheduling, not attribution to a saved estimator prediction",
            "cost_scope": "No model calls; native model-cost totals are empty/zero. Whole-workload operating cost remains unmeasured in the ablation.",
        },
    )
    (out / "report.md").write_text(ablation_to_markdown(report), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = export(args.plan, args.out)
    print(
        json.dumps(
            {
                key: report[key]
                for key in ("planned_observation_count", "observed_count", "status_counts")
            }
        )
    )
