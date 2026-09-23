"""Read retained study artifacts, preserving planned denominators and predictions."""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from statistics import fmean

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "real_agent_study"

from agentloop.html_report import analysis_to_html
from agentloop.interventions import build_intervention
from agentloop.quality import build_quality_report
from agentloop.replay import ReplayGates, build_replay_report
from agentloop.studies import _bootstrap, study_to_markdown, summarize_study, summarize_values
from agentloop.tracer import AgentTrace

from .run import fingerprint, load_protocol, write_new
from .scoring import SCORER_VERSION, replay_scorer


def read_case(folder, protocol_hash, task, condition, repetition):
    folder = Path(folder)
    if not (folder / "receipt.json").exists():
        return {
            "availability": "incomplete" if (folder / "attempt.json").exists() else "missing",
            "receipt": None,
            "trace": None,
            "diagnosis": None,
        }
    receipt = json.loads((folder / "receipt.json").read_text(encoding="utf-8"))
    expected = {
        "protocol_hash": protocol_hash,
        "task_id": task["id"],
        "workload": task["workload"],
        "condition": condition,
        "repetition": repetition,
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise ValueError("run identity does not match its planned slot")
    trace, diagnosis = None, None
    if receipt["traced"]:
        trace = AgentTrace.from_json(folder / "trace.json")
        diagnosis = json.loads((folder / "diagnosis.json").read_text(encoding="utf-8"))
        if (
            fingerprint(trace.to_dict()) != receipt["trace_hash"]
            or fingerprint(diagnosis) != receipt["diagnosis_hash"]
            or diagnosis["run_id"] != trace.run_id
            or any(trace.metadata.get(key) != value for key, value in expected.items())
            or trace.metadata.get("output") != receipt["output"]
            or trace.metadata.get("quality_score") != receipt["quality"]["score"]
            or trace.metadata.get("success") != receipt["quality"]["passed"]
        ):
            raise ValueError("trace or original prediction evidence changed")
    return {"availability": "recorded", "receipt": receipt, "trace": trace, "diagnosis": diagnosis}


def selected_tasks(protocol, split):
    return [
        task
        for task in protocol["tasks"]
        if task["split"] == split
        and (split != "evaluation" or task["id"] in protocol["evaluation_task_ids"])
    ]


def case_path(root, split, condition, repetition, identity, *, traced=True):
    return (
        Path(root)
        / "runs"
        / split
        / condition
        / f"rep-{repetition}-{'traced' if traced else 'untraced'}"
        / identity
    )


def freeze_evaluation(pilot_path, root, destination):
    root = Path(root)
    protocol = load_protocol(pilot_path, root / "sources")
    if protocol["phase"] != "pilot":
        raise ValueError("evaluation is frozen from a pilot protocol")
    pilot_hash = fingerprint(protocol)
    durations, receipts, findings = [], [], []
    for task in selected_tasks(protocol, "pilot"):
        for condition in ("baseline", "candidate"):
            row = read_case(
                case_path(root, "pilot", condition, 0, task["id"]), pilot_hash, task, condition, 0
            )
            if row["availability"] != "recorded":
                raise ValueError("all planned pilot attempts must be retained before selection")
            receipt = row["receipt"]
            receipts.append(
                {
                    "task_id": task["id"],
                    "condition": condition,
                    "receipt_hash": fingerprint(receipt),
                    "status": receipt["status"],
                    "elapsed_ms": receipt["elapsed_ms"],
                }
            )
            if receipt["status"] == "completed":
                durations.append(receipt["elapsed_ms"])
            if condition == "baseline":
                findings.extend(row["diagnosis"]["findings"])
    if not any(item["type"] == "route_to_smaller_model" for item in findings):
        raise ValueError("pilot does not support selecting the proposed routing intervention")
    count = 4 if durations and max(durations) > 60000 else 6
    candidates = [task for task in protocol["tasks"] if task["split"] == "evaluation"]
    selected = [
        task["id"]
        for workload in ("repository", "sql", "math")
        for task in [item for item in candidates if item["workload"] == workload][:count]
    ]
    protocol.update(
        phase="evaluation",
        warmup="One fixed non-task JSON readiness request after each model-server start; retained separately and excluded from task latency. Prompt caching disabled for every request.",
        pilot_protocol_hash=pilot_hash,
        pilot_receipts=receipts,
        evaluation_task_ids=selected,
        evaluation_excluded_tasks=[
            {"task_id": task["id"], "reason": "preregistered pilot resource rule"}
            for task in candidates
            if task["id"] not in selected
        ],
        selection={
            "family": "route_to_smaller_model",
            "scope": "replace every agent_decision model call with the pinned smaller model; retain all other application code",
            "basis": "baseline pilot routing findings; evaluate independently of whether the pilot quality improved",
            "task_count_per_workload": count,
            "slowest_completed_pilot_ms": max(durations) if durations else None,
        },
    )
    write_new(destination, protocol)
    return protocol


def task_uncertainty(pairs):
    by_task = defaultdict(list)
    for task_id, delta in pairs:
        by_task[task_id].append(delta)
    means = [fmean(values) for values in by_task.values()]
    return {
        "independent_tasks": len(means),
        "pairs": len(pairs),
        "task_mean_deltas_ms": {key: fmean(value) for key, value in sorted(by_task.items())},
        "summary": summarize_values(means),
        "bootstrap": _bootstrap(means, {"samples": 1000, "seed": 20260923, "confidence": 0.95}),
        "limits": "Resamples observed task means, not repetitions as independent tasks. Descriptive for this small selected task set; fixed condition order and shared hardware remain confounds.",
    }


def report(protocol_path, root, out, *, split="evaluation"):
    root, out = Path(root).resolve(), Path(out).resolve()
    if out.exists() and any(out.iterdir()):
        raise ValueError("reports require a fresh or empty output directory")
    protocol = load_protocol(protocol_path, root / "sources")
    protocol_hash = fingerprint(protocol)
    tasks = selected_tasks(protocol, split)
    repetitions = protocol["repetitions"] if split == "evaluation" else 1
    sources = json.loads((root / "sources/agentloop-v070-source.json").read_text(encoding="utf-8"))
    out.mkdir(parents=True, exist_ok=True)
    rows, selections, intervals, quality_intervals = [], [], defaultdict(list), defaultdict(list)
    native_paths = defaultdict(lambda: defaultdict(list))
    html_done = set()
    for task in tasks:
        for repetition in range(repetitions):
            pair = {
                condition: read_case(
                    case_path(root, split, condition, repetition, task["id"]),
                    protocol_hash,
                    task,
                    condition,
                    repetition,
                )
                for condition in ("baseline", "candidate")
            }
            row = {
                "task_id": task["id"],
                "workload": task["workload"],
                "repetition": repetition,
                "availability": {key: value["availability"] for key, value in pair.items()},
            }
            for condition, value in pair.items():
                receipt = value["receipt"]
                row[condition] = (
                    None
                    if receipt is None
                    else {
                        key: receipt[key]
                        for key in (
                            "status",
                            "quality",
                            "elapsed_ms",
                            "recording_ms",
                            "operating_cost_usd",
                            "paid_provider_spend_usd",
                        )
                    }
                )
                if value["trace"] is not None:
                    native = out / task["workload"] / condition / f"{task['id']}-{repetition}.json"
                    value["trace"].export_json(native)
                    native_paths[task["workload"]][condition].append(
                        native.relative_to(out / task["workload"]).as_posix()
                    )
            if all(value["trace"] is not None for value in pair.values()):
                before, after = pair["baseline"], pair["candidate"]
                fixture = {
                    "id": task["id"],
                    "expected": task["expected"],
                    "task": task,
                    "sources": sources if task["workload"] == "repository" else {},
                    "baseline_output": before["receipt"]["output"],
                    "candidate_output": after["receipt"]["output"],
                    "scorer": {
                        "type": "custom",
                        "callable": replay_scorer.__module__ + ":replay_scorer",
                        "version": SCORER_VERSION,
                    },
                }
                quality = build_quality_report([fixture], min_score=1)
                gates = ReplayGates(min_quality_score=1)
                replay = build_replay_report(
                    before["trace"], after["trace"], gates=gates, quality_report=quality
                )
                row.update(
                    quality=quality,
                    native_gates=replay["gates"],
                    latency_delta_ms=after["receipt"]["elapsed_ms"]
                    - before["receipt"]["elapsed_ms"],
                )
                intervals[task["workload"]].append((task["id"], row["latency_delta_ms"]))
                if (
                    quality["candidate_score"] == 1
                    and quality["candidate_score"] >= quality["baseline_score"]
                ):
                    quality_intervals[task["workload"]].append(
                        (task["id"], row["latency_delta_ms"])
                    )
                targets = []
                for finding in before["diagnosis"]["findings"]:
                    selected = finding["type"] == "route_to_smaller_model"
                    if selected:
                        targets.append(finding["finding_id"])
                    selections.append(
                        {
                            "task_id": task["id"],
                            "repetition": repetition,
                            "finding_id": finding["finding_id"],
                            "type": finding["type"],
                            "status": "selected" if selected else "rejected",
                            "reason": "preregistered model-routing experiment"
                            if selected
                            else "outside selected intervention; no benefit assumed",
                            "workload_review": "Repeated agent decisions consume previous tool results, so batching those sequential decisions is not a valid rewrite."
                            if finding["type"] == "batch_model_calls"
                            else None,
                        }
                    )
                if targets:
                    record = build_intervention(
                        before["trace"],
                        after["trace"],
                        target_finding_ids=targets,
                        intervention_type="model_routing",
                        configuration={
                            "baseline": protocol["models"]["baseline"],
                            "candidate": protocol["models"]["candidate"],
                            "scope": "all model calls; original findings can cover a subset of those calls",
                        },
                        metadata={
                            "synthetic": False,
                            "task_id": task["id"],
                            "workload": task["workload"],
                            "repetition": repetition,
                            "protocol_hash": protocol_hash,
                            "prediction_source": "archived before candidate execution",
                        },
                        diagnosis=before["diagnosis"],
                        gates=gates,
                        quality_report=quality,
                        replay_report=replay,
                    ).to_dict()
                    write_new(out / "interventions" / f"{record['intervention_id']}.json", record)
                    row["intervention_id"] = record["intervention_id"]
                else:
                    row["intervention_id"] = None
                    row["unselected_reason"] = "no eligible original routing finding"
                write_new(out / "replay" / f"{task['id']}-{repetition}.json", replay)
                if task["workload"] not in html_done:
                    payload = {
                        "trace": before["trace"].to_dict(),
                        "report": before["trace"].report(),
                        "diagnosis": before["diagnosis"],
                        "optimization": json.loads(
                            (
                                case_path(root, split, "baseline", repetition, task["id"])
                                / "plan.json"
                            ).read_text()
                        ),
                        "replay": replay,
                    }
                    (out / f"{task['workload']}.html").write_text(
                        analysis_to_html(payload), encoding="utf-8"
                    )
                    html_done.add(task["workload"])
            rows.append(row)
    for workload, conditions in native_paths.items():
        if set(conditions) == {"baseline", "candidate"}:
            manifest = {
                "schema_version": "1.0",
                "name": f"Exploratory {workload} routing",
                "baseline": "baseline",
                "conditions": dict(conditions),
                "pairing_keys": protocol["pairing_keys"],
                "bootstrap": {"samples": 1000, "seed": 20260923, "confidence": 0.95},
            }
            path = out / workload / "study.json"
            write_new(path, manifest)
            study = summarize_study(path)
            write_new(out / workload / "study-results.json", study)
            (out / workload / "study.md").write_text(study_to_markdown(study), encoding="utf-8")
    summary = {
        "protocol_hash": protocol_hash,
        "split": split,
        "planned_pairs": len(tasks) * repetitions,
        "paired_count": sum("quality" in row for row in rows),
        "conditions": {},
        "latency": {workload: task_uncertainty(values) for workload, values in intervals.items()},
        "quality_preserving_latency": {
            workload: task_uncertainty(values) for workload, values in quality_intervals.items()
        },
        "native_gate_passes": sum(row.get("native_gates", {}).get("passed", False) for row in rows),
        "rows": rows,
        "finding_selections": selections,
        "limits": protocol["limits"],
        "cost_scope": protocol["cost_scope"],
    }
    for condition in ("baseline", "candidate"):
        observed = [row[condition] for row in rows if row[condition] is not None]
        summary["conditions"][condition] = {
            "planned": len(rows),
            "recorded": len(observed),
            "missing_or_incomplete": len(rows) - len(observed),
            "statuses": dict(Counter(row["status"] for row in observed)),
            "quality_successes": sum(row["quality"]["passed"] for row in observed),
            "unknown_operating_costs": sum(row["operating_cost_usd"] is None for row in observed),
            "recording_ms": summarize_values([row["recording_ms"] for row in observed]),
        }
    write_new(out / "summary.json", summary)
    return summary


def read_bundle(path):
    """Validate an offline ZIP without extracting files or executing its contents."""
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        if (
            len(names) != len(set(names))
            or len(names) > 10000
            or sum(item.file_size for item in infos) > 100_000_000
        ):
            raise ValueError("duplicate or oversized bundle")
        for name in names:
            if (
                "\\" in name
                or name.startswith("/")
                or ":" in name
                or ".." in Path(name).parts
                or Path(name).as_posix() != name
            ):
                raise ValueError("unsafe bundle path")
        manifest = json.loads(archive.read("bundle-manifest.json"))
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != "1.0"
            or not isinstance(manifest.get("files"), dict)
        ):
            raise ValueError("unsupported evidence bundle manifest")
        if set(manifest["files"]) != set(names) - {"bundle-manifest.json"}:
            raise ValueError("bundle manifest does not cover every file")
        import hashlib

        for name, expected in manifest["files"].items():
            if hashlib.sha256(archive.read(name)).hexdigest() != expected:
                raise ValueError("bundle content checksum mismatch")
        return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("freeze-evaluation", "report"))
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--split", default="evaluation", choices=("pilot", "evaluation"))
    args = parser.parse_args()
    result = (
        freeze_evaluation(args.protocol, args.root, args.out)
        if args.command == "freeze-evaluation"
        else report(args.protocol, args.root, args.out, split=args.split)
    )
    print(
        json.dumps(
            {
                key: result[key]
                for key in ("phase", "planned_pairs", "paired_count", "conditions")
                if key in result
            }
        ),
        flush=True,
    )
