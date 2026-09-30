"""Recompute independent quality and preserve every planned ablation slot."""

from __future__ import annotations

import json
from pathlib import Path

from agentloop.ablations import ablation_to_markdown, build_ablation_report, summarize_ablation
from agentloop.findings import build_diagnosis
from agentloop.html_report import analysis_to_html
from agentloop.optimizer import build_optimization_plan
from agentloop.tracer import AgentTrace
from examples.real_agent_study.run import fingerprint, write_new
from examples.real_agent_study.scoring import grade

from .protocol import load_plan


def export(plan_path, out):
    plan_path, out = Path(plan_path), Path(out)
    plan = load_plan(plan_path)
    if out.exists() and any(out.iterdir()):
        raise ValueError("export directory must be fresh or empty")
    spec = plan["protocol"]["specification"]
    tasks = {task["id"]: task for task in plan["tasks"]}
    expected = {}
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
            expected[identity] = {
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
    if attempts.exists() and any(path.name not in expected for path in attempts.iterdir()):
        raise ValueError("unplanned attempt; retain it for explicit review")
    rows, manifests, hashes, unavailable, validated = [], {}, {}, [], []
    for identity, slot in expected.items():
        folder = attempts / identity
        path = folder / "receipt.json"
        if not path.exists():
            unavailable.append(
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
        if row["status"] == "cancelled":
            if (
                row["success"] is not None
                or row["quality_score"] is not None
                or receipt["quality"] is not None
                or any(value is not None for value in row["metrics"].values())
            ):
                raise ValueError("cancelled task cannot claim complete measurements")
        else:
            quality = grade(
                receipt["answer"],
                tasks[row["task_id"]],
                sources={},
                tool_history=receipt["tool_history"],
            )
            if (
                quality != receipt["quality"]
                or quality["score"] != row["quality_score"]
                or quality["passed"] != row["success"]
            ):
                raise ValueError("independent quality evidence changed")
            if row["status"] != "completed" and row["success"]:
                raise ValueError("unfinished task cannot pass quality")
            calls = receipt["model_calls"]
            tokens = (
                sum(
                    call["usage"]["prompt_tokens"] + call["usage"]["completion_tokens"]
                    for call in calls
                )
                if all(call["usage"] is not None for call in calls)
                else None
            )
            if (
                row["metrics"]["model_calls"] != len(calls)
                or row["metrics"]["tool_calls"] != len(receipt["tool_history"])
                or row["metrics"]["tokens"] != tokens
            ):
                raise ValueError("resource totals diverged from recorded calls")
        if (
            row["metrics"]["cost_usd"] is not None
            or row["cost_status"] != "unknown"
            or row["cost_basis"] != "unknown"
        ):
            raise ValueError("self-hosted operating cost was not measured")
        trace = None
        if (row["trace_run_id"] is None) != (slot["condition"] == "off"):
            raise ValueError("trace presence contradicts the planned condition")
        if row["trace_run_id"] is not None:
            trace = AgentTrace.from_json(folder / "trace.json")
            if (
                fingerprint(trace.to_dict()) != receipt["trace_hash"]
                or trace.run_id != row["trace_run_id"]
            ):
                raise ValueError("trace changed")
            for key, field in (
                ("agentloop.harness", "policy_evidence"),
                ("agentloop.context_transform", "context_evidence"),
            ):
                if trace.metadata.get(key) != receipt[field]:
                    raise ValueError("native evidence diverged from receipt")
            if (
                trace.metadata.get("success") != row["success"]
                or trace.metadata.get("quality_score") != row["quality_score"]
            ):
                raise ValueError("trace quality diverged from receipt")
        elif (
            slot["condition"] != "off"
            or (folder / "trace.json").exists()
            or receipt["policy_evidence"] is not None
            or receipt["context_evidence"] is not None
        ):
            raise ValueError("missing or fabricated native trace evidence")
        rows.append(row)
        validated.append((row, trace))
        hashes[identity] = fingerprint(receipt)
    # Validate every observation before writing any derived output.
    build_ablation_report(plan["protocol"], rows)
    out.mkdir(parents=True, exist_ok=True)
    html_done = set()
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
                "observations": rows,
                "study_manifest": "study.json",
                "interventions": [],
            },
        )
        report = summarize_ablation(out / "bundle.json")
    else:
        report = build_ablation_report(plan["protocol"], rows)
        report["artifact_links_complete"] = False
        report["artifact_link_reason"] = (
            "A linked native study needs a recorded tracing baseline and at least two traced conditions; all planned slots remain in the ablation denominator."
        )
        write_new(out / "observations.json", rows)
    write_new(out / "report.json", report)
    write_new(
        out / "provenance.json",
        {
            "receipt_hashes": hashes,
            "unavailable": unavailable,
            "ledger_scope": "Caller-configured intervention, not attribution to an estimator finding.",
        },
    )
    (out / "report.md").write_text(ablation_to_markdown(report), encoding="utf-8")
    return report
