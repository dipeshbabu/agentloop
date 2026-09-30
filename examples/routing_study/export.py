"""Read-only independent grading and native paired routing reports."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from agentloop.findings import build_diagnosis
from agentloop.html_report import analysis_to_html
from agentloop.interventions import build_intervention
from agentloop.optimizer import build_optimization_plan
from agentloop.quality import build_quality_report
from agentloop.replay import ReplayGates, build_replay_report
from agentloop.studies import study_to_markdown, summarize_study
from agentloop.study_statistics import task_weighted_summary
from agentloop.tracer import AgentTrace
from examples.real_agent_study.run import MODELS, fingerprint, write_new
from examples.real_calibration_study import normalize_answer

from .protocol import load_plan
from .runtime import identity


def _read(folder, plan, task, condition, repetition):
    path = folder / "receipt.json"
    if not path.exists():
        return {
            "availability": "incomplete" if (folder / "started.json").exists() else "not_recorded"
        }
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if any(
        receipt.get(key) != value
        for key, value in {
            "plan_hash": plan["plan_hash"],
            "task_id": task["id"],
            "condition": condition,
            "repetition": repetition,
        }.items()
    ):
        raise ValueError("receipt does not match frozen slot")
    if receipt["status"] not in {"completed", "failed", "cancelled"}:
        raise ValueError("unknown task outcome")
    raw = None
    if receipt["calls"] and receipt["calls"][-1]["output_text"] is not None:
        try:
            value = json.loads(receipt["calls"][-1]["output_text"])
            raw = normalize_answer(value.get("answer")) if isinstance(value, dict) else None
        except ValueError:
            pass
    if receipt["status"] == "completed" and raw != receipt["answer"]:
        raise ValueError("answer differs from original model output")
    score = (
        None
        if receipt["status"] == "cancelled"
        else float(
            receipt["status"] == "completed"
            and receipt["answer"] is not None
            and receipt["answer"] == task["expected"]
        )
    )
    if score != receipt["quality_score"]:
        raise ValueError("independent task quality changed")
    trace = AgentTrace.from_json(folder / "trace.json")
    if (
        fingerprint(trace.to_dict()) != receipt["trace_hash"]
        or trace.metadata["quality_score"] != score
        or trace.metadata["output"] != receipt["answer"]
        or trace.metadata["success"] != (bool(score) if score is not None else None)
    ):
        raise ValueError("trace or quality changed")
    if (
        trace.metadata.get("agentloop.model_routing") != receipt["routing"]
        or trace.metadata.get("agentloop.harness") != receipt["harness"]
    ):
        raise ValueError("native routing/harness evidence changed")
    model_events = [event for event in trace.events if event.event_type == "model_call"]
    if len(model_events) != len(receipt["calls"]):
        raise ValueError("model call evidence changed")
    for event, call in zip(model_events, receipt["calls"]):
        if event.model != identity(condition).model or event.output_text != call["output_text"]:
            raise ValueError("model identity/output diverged")
        usage = call["usage"]
        if usage is not None and (
            event.input_tokens != usage["prompt_tokens"]
            or event.output_tokens != usage["completion_tokens"]
        ):
            raise ValueError("model usage diverged")
        if call.get("token_count_matches_provider") is False and receipt["status"] == "completed":
            raise ValueError("unverified context count cannot be a completed task")
    if receipt["status"] == "cancelled":
        if receipt["decision_latency_ms"] is not None:
            raise ValueError("cancelled task cannot claim complete latency")
    elif not isinstance(receipt["decision_latency_ms"], (int, float)) or receipt[
        "decision_latency_ms"
    ] + 1e-6 < sum(event.duration_ms for event in model_events):
        raise ValueError("decision latency cannot omit provider time")
    if receipt["operating_cost_usd"] is not None or receipt["paid_provider_spend_usd"] != 0:
        raise ValueError("study did not measure operating cost or use a paid provider")
    return {"availability": "recorded", "receipt": receipt, "trace": trace, "folder": folder}


def export(plan_path, out):
    plan_path, out = Path(plan_path), Path(out)
    plan = load_plan(plan_path)
    if out.exists() and any(out.iterdir()):
        raise ValueError("export directory must be fresh")
    root = plan_path.parent
    expected = {
        f"{condition}/{rep}/{task['id']}"
        for condition in MODELS
        for rep in range(plan["configuration"]["repetitions"])
        for task in plan["tasks"]
    }
    parents = {str(Path(path).parent).replace("\\", "/") for path in expected} | set(MODELS)
    if (root / "attempts").exists():
        for path in (root / "attempts").rglob("*"):
            relative = path.relative_to(root / "attempts").as_posix()
            if path.is_dir() and relative not in expected | parents:
                raise ValueError("unplanned attempt directory")
            if (
                path.is_file()
                and path.parent.relative_to(root / "attempts").as_posix() not in expected
            ):
                raise ValueError("unplanned attempt artifact")
    cases, manifests, hashes, intervals, eligible = [], {name: [] for name in MODELS}, {}, [], []
    stored, html_done = [], set()
    status_counts = Counter()
    for task in plan["tasks"]:
        for repetition in range(plan["configuration"]["repetitions"]):
            pair = {
                condition: _read(
                    root / "attempts" / condition / str(repetition) / task["id"],
                    plan,
                    task,
                    condition,
                    repetition,
                )
                for condition in MODELS
            }
            row = {"task_id": task["id"], "split": task["split"], "repetition": repetition}
            for condition, result in pair.items():
                row[condition] = {"availability": result["availability"]}
                if result["availability"] == "recorded":
                    receipt = result["receipt"]
                    row[condition].update(
                        status=receipt["status"],
                        quality_score=receipt["quality_score"],
                        decision_latency_ms=receipt["decision_latency_ms"],
                        model_calls=len(receipt["calls"]),
                    )
                    status_counts[receipt["status"]] += 1
                    hashes[f"{condition}/{repetition}/{task['id']}"] = fingerprint(receipt)
                    stored.append((condition, repetition, task["id"], result["trace"]))
            if all(value["availability"] == "recorded" for value in pair.values()):
                baseline, candidate = pair["baseline"], pair["candidate"]
                prediction = json.loads(
                    (baseline["folder"] / "prediction.json").read_text(encoding="utf-8")
                )
                journal = json.loads(
                    (baseline["folder"] / "journal.json").read_text(encoding="utf-8")
                )
                if journal["prediction_hash"] != fingerprint(prediction) or datetime.fromisoformat(
                    journal["prediction_recorded_at"]
                ) > datetime.fromisoformat(candidate["trace"].started_at):
                    raise ValueError("prediction was not preserved before candidate measurement")
                quality = build_quality_report(
                    [
                        {
                            "id": task["id"],
                            "expected": task["expected"],
                            "baseline_output": baseline["receipt"]["answer"],
                            "candidate_output": candidate["receipt"]["answer"],
                        }
                    ],
                    min_score=1,
                )
                replay = build_replay_report(
                    baseline["trace"],
                    candidate["trace"],
                    gates=ReplayGates(min_quality_score=1),
                    quality_report=quality,
                )
                row["native_gates_passed"] = replay["gates"]["passed"]
                row["quality_loss"] = (
                    baseline["receipt"]["quality_score"] == 1
                    and candidate["receipt"]["quality_score"] == 0
                )
                row["both_correct"] = (
                    baseline["receipt"]["quality_score"]
                    == candidate["receipt"]["quality_score"]
                    == 1
                )
                if prediction["selected"]:
                    diagnosis = json.loads(
                        (baseline["folder"] / "diagnosis.json").read_text(encoding="utf-8")
                    )
                    actual = json.loads(
                        (candidate["folder"] / "intervention.json").read_text(encoding="utf-8")
                    )
                    rebuilt = build_intervention(
                        baseline["trace"],
                        candidate["trace"],
                        target_finding_ids=[prediction["selected"][0]["finding_id"]],
                        intervention_type="explicit_model_routing",
                        configuration={
                            "original": MODELS["baseline"],
                            "target": MODELS["candidate"],
                            "scope": "one answer model call with capability checks",
                        },
                        diagnosis=diagnosis,
                        gates=ReplayGates(min_quality_score=1),
                        quality_report=quality,
                        metadata={
                            "synthetic": False,
                            "task_id": task["id"],
                            "repetition": repetition,
                            "protocol_hash": plan["plan_hash"],
                        },
                    ).to_dict()
                    if (
                        rebuilt != actual
                        or actual["predicted"]["findings"] != prediction["selected"]
                    ):
                        raise ValueError("original intervention evidence changed")
                    row["intervention_id"] = actual["intervention_id"]
                times = [pair[name]["receipt"]["decision_latency_ms"] for name in MODELS]
                row["latency_delta_ms"] = (
                    times[1] - times[0] if all(value is not None for value in times) else None
                )
            else:
                row.update(
                    native_gates_passed=None,
                    quality_loss=None,
                    both_correct=False,
                    latency_delta_ms=None,
                )
            if task["split"] == "held_out":
                intervals.append((task["id"], row["latency_delta_ms"]))
                eligible.append(
                    (task["id"], row["latency_delta_ms"] if row["both_correct"] else None)
                )
            cases.append(row)
    out.mkdir(parents=True, exist_ok=True)
    for condition, repetition, task_id, trace in stored:
        target = out / "traces" / condition / f"{task_id}-{repetition}.json"
        trace.export_json(target)
        if next(task for task in plan["tasks"] if task["id"] == task_id)["split"] == "held_out":
            manifests[condition].append(target.relative_to(out).as_posix())
        if condition not in html_done:
            payload = {
                "trace": trace.to_dict(),
                "report": trace.report(),
                "diagnosis": build_diagnosis(trace),
                "optimization": build_optimization_plan(trace),
            }
            (out / f"{condition}.html").write_text(analysis_to_html(payload), encoding="utf-8")
            html_done.add(condition)
    if all(manifests.values()):
        write_new(
            out / "study.json",
            {
                "schema_version": "1.0",
                "name": "Capability-checked routing: held-out tasks",
                "baseline": "baseline",
                "conditions": manifests,
                "pairing_keys": ["task_id", "repetition", "protocol_hash"],
            },
        )
        native = summarize_study(out / "study.json")
        write_new(out / "native-study.json", native)
        (out / "native-study.md").write_text(study_to_markdown(native), encoding="utf-8")
    held = [row for row in cases if row["split"] == "held_out"]
    settings = {"samples": 1000, "seed": 20260930, "confidence": 0.95}
    summary = {
        "schema_version": "1.0",
        "plan_hash": plan["plan_hash"],
        "planned_trials": len(expected),
        "observed_trials": len(hashes),
        "status_counts": dict(status_counts),
        "cases": cases,
        "held_out_pairs": len(held),
        "quality_losses": sum(row["quality_loss"] is True for row in held),
        "all_candidate_tasks_correct": all(
            row["candidate"].get("quality_score") == 1 for row in held
        ),
        "latency": task_weighted_summary(intervals, settings),
        "both_correct_latency": task_weighted_summary(eligible, settings),
        "paid_provider_spend_usd": 0,
        "operating_cost_usd": None,
        "limits": plan["limits"],
    }
    write_new(out / "summary.json", summary)
    write_new(
        out / "provenance.json", {"receipt_hashes": hashes, "all_planned_slots_retained": True}
    )
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = export(args.plan, args.out)
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "planned_trials",
                    "observed_trials",
                    "status_counts",
                    "quality_losses",
                    "all_candidate_tasks_correct",
                )
            }
        )
    )
