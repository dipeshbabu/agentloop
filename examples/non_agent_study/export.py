"""Publish raw outcomes plus native finding-quality and estimator-calibration inputs."""

from __future__ import annotations

import json
import shutil
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agentloop.calibration import calibration_to_markdown, summarize_calibration
from agentloop.finding_benchmark_types import FindingBenchmark
from agentloop.finding_benchmarks import finding_benchmark_markdown, run_finding_benchmark
from agentloop.interventions import InterventionRecord
from agentloop.studies import _bootstrap, summarize_values
from agentloop.tracer import AgentTrace
from examples.non_agent_study.data import digest, read, write_new
from examples.non_agent_study.workloads import quality_report


def _verified(path):
    value = read(path)
    owned = dict(value)
    if owned.pop("sha256") != digest(owned):
        raise ValueError("study artifact hash mismatch")
    return value


def validate_pair(root, protocol, workload, task, repetition, variant):
    folder = root / workload["id"] / task["id"] / str(repetition)
    target = folder / variant
    if not (target / "receipt.json").exists():
        return None
    receipt = _verified(target / "receipt.json")
    before, after = (
        AgentTrace.from_json(folder / "baseline.json"),
        AgentTrace.from_json(target / "candidate.json"),
    )
    prediction, selections, journal = (
        read(folder / "prediction.json"),
        read(folder / "selections.json"),
        read(folder / "prediction-journal.json"),
    )
    if (
        receipt["protocol_hash"] != protocol["sha256"]
        or receipt["task_sha256"] != digest(task)
        or receipt["baseline_hash"] != digest(before.to_dict())
        or receipt["candidate_hash"] != digest(after.to_dict())
    ):
        raise ValueError("study input/trace binding mismatch")
    if (
        journal != receipt["prediction_journal"]
        or journal["prediction_hash"] != digest(prediction)
        or journal["selection_hash"] != digest(selections)
    ):
        raise ValueError("study original prediction changed")
    if journal["prediction_recorded_at"] > receipt["candidate_started_at"]:
        raise ValueError("prediction was not frozen before candidate")
    quality = quality_report(workload, task, before, after)
    if quality != receipt["quality"] or quality != read(target / "quality.json"):
        raise ValueError("quality differs from pinned labels/scorer")
    record = (
        InterventionRecord.from_dict(read(target / "intervention.json")).to_dict()
        if receipt["intervention_id"]
        else None
    )
    replay = read(target / "replay.json")
    if (
        receipt["deltas"] != replay["deltas"]
        or receipt["native_gates"] != replay["gates"]
        or record is not None
        and (
            record["measured"] != replay
            or record["trace_fingerprints"]
            != {"baseline": digest(before.to_dict()), "candidate": digest(after.to_dict())}
        )
    ):
        raise ValueError("native intervention/replay changed")
    a, b = quality["baseline_score"], quality["candidate_score"]
    accepted = (
        a is not None and b is not None and b >= workload["quality"]["minimum_score"] and b >= a
    )
    if accepted != receipt["owner_quality_gate_passed"]:
        raise ValueError("owner quality gate mismatch")
    controls = [
        AgentTrace.from_json(folder / "baseline-control.json"),
        AgentTrace.from_json(target / "candidate-control.json"),
    ]
    if (
        controls[0].metadata["output"] != before.metadata["output"]
        or controls[1].metadata["output"] != after.metadata["output"]
    ):
        raise ValueError("recording control output mismatch")
    if controls[1].elapsed_ms - controls[0].elapsed_ms != receipt["unrecorded_runtime_delta_ms"]:
        raise ValueError("recording control timing mismatch")
    return receipt


def _labels(trace, task, workload):
    groups = defaultdict(list)
    for event in trace.events:
        if event.event_type == "model_call":
            groups[event.name].append(event.event_id)
    opportunities = [
        {"family": "batch_model_calls", "spans": ids} for ids in groups.values() if len(ids) >= 3
    ]
    definitions = [
        (
            "batch_model_calls",
            "opportunity" if opportunities else "no_opportunity",
            opportunities,
            "Rows are independent learned-model evaluations; dependencies only select each row's fallback. The vectorized candidate preserves row order and the same learned parameters.",
            ["timing"],
        ),
        (
            "route_to_smaller_model",
            "unknown",
            [],
            "Numeric models have no language-token usage. Zero placeholders cannot justify token-based routing; the cheaper-model experiment requires independent quality evidence.",
            ["timing", "tokens"],
        ),
    ]
    return [
        {
            "id": task["id"] + ":" + rule,
            "workload_ref": workload["id"],
            "group": workload["id"],
            "split": "evaluation",
            "scenario": workload["category"],
            "rule_id": rule,
            "label": label,
            "label_ref": "nonagent-independent-row-contract:v1:" + workload["id"],
            "label_version": "1.0",
            "rationale": rationale,
            "opportunities": expected,
            "required_evidence": required,
            "trace": trace.to_dict(),
        }
        for rule, label, expected, rationale, required in definitions
    ]


def export(plan, fit, held_out, out):
    plan, fit, held_out, out = map(Path, (plan, fit, held_out, out))
    protocol = _verified(plan / "protocol.json")
    out.mkdir(parents=True, exist_ok=False)
    shutil.copytree(plan, out / "plan")
    shutil.copytree(fit, out / "fit")
    shutil.copytree(held_out, out / "held_out")
    cases, registrations, rows, emission, exclusions = [], [], [], [], []
    environment = read(fit / "environment.json")
    environment.pop("phase", None)
    complete_inventory = True
    for workload in protocol["workloads"]:
        for task in workload["tasks"]:
            phase = task["split"]
            root = out / phase
            for repetition in range(workload["repetitions"]):
                folder = root / workload["id"] / task["id"] / str(repetition)
                if not all(
                    (folder / name).exists()
                    for name in (
                        "prediction.json",
                        "prediction-journal.json",
                        "selections.json",
                        "baseline.json",
                    )
                ):
                    complete_inventory = False
                    for variant in workload["variants"]:
                        rows.append(
                            {
                                "workload": workload["id"],
                                "task_id": task["id"],
                                "split": phase,
                                "variant": variant,
                                "repetition": repetition,
                                "availability": "missing_baseline",
                            }
                        )
                    continue
                prediction, journal, selections = (
                    read(folder / "prediction.json"),
                    read(folder / "prediction-journal.json"),
                    read(folder / "selections.json"),
                )
                trace = AgentTrace.from_json(folder / "baseline.json")
                if phase == "held_out" and repetition == 0:
                    cases += _labels(trace, task, workload)
                emission.append(
                    {
                        "workload": workload["id"],
                        "task_id": task["id"],
                        "split": phase,
                        "repetition": repetition,
                        "findings": selections,
                        "analysis_ms": journal["analysis_ms"],
                        "capture_validation": journal["validation"],
                        "baseline_recording_overhead_ms": journal["baseline_recording_overhead_ms"],
                    }
                )
                for variant in workload["variants"]:
                    receipt = validate_pair(root, protocol, workload, task, repetition, variant)
                    if receipt is None:
                        rows.append(
                            {
                                "workload": workload["id"],
                                "task_id": task["id"],
                                "split": phase,
                                "variant": variant,
                                "repetition": repetition,
                                "availability": "missing",
                            }
                        )
                        continue
                    rows.append({**receipt, "availability": "recorded"})
                    target = folder / variant
                    record = (
                        read(target / "intervention.json") if receipt["intervention_id"] else None
                    )
                    configuration = (
                        record["configuration"]
                        if record
                        else {
                            "variant": variant,
                            "models": workload["models"],
                            "attribution": workload["attribution"][variant],
                            "protocol_hash": protocol["sha256"],
                        }
                    )
                    context = {
                        "workload": workload["id"] + ":" + workload["prepared_sha256"],
                        "model": "sha256:" + digest(workload["models"]),
                        "provider": "local-numpy:" + protocol["numpy_version"],
                        "environment": "sha256:" + digest(environment),
                        "scorer": workload["quality"]["scorer"]
                        + ":1.0:"
                        + workload["prepared_sha256"],
                        "policy": None,
                        "cost_basis": "unknown",
                        "pricing": None,
                        "quality_gate": {
                            "min_score": workload["quality"]["minimum_score"],
                            "max_regression": 0,
                        },
                        "intervention": {
                            "type": "numeric_model_" + variant,
                            "configuration": configuration,
                        },
                    }
                    status = {item["finding_id"]: item for item in selections[variant]}
                    for index, finding in enumerate(prediction["findings"]):
                        chosen = status[finding["finding_id"]]["selection"] == "selected"
                        compatible = (
                            chosen
                            and record is not None
                            and workload["attribution"][variant] == "isolated_batching"
                            and len(record["target_finding_ids"]) == 1
                        )
                        if chosen and not compatible:
                            exclusions.append(
                                {
                                    "case_id": f"{task['id']}-{repetition}-{variant}-{index}",
                                    "reason": "combined_scope_not_attributed",
                                    "intervention": (target / "intervention.json")
                                    .relative_to(out)
                                    .as_posix()
                                    if record
                                    else None,
                                    "outcome_retained": record is not None,
                                }
                            )
                        registrations.append(
                            {
                                "case_id": f"{task['id']}-{repetition}-{variant}-{index}",
                                "task_id": task["id"],
                                "task_sha256": digest(task),
                                "repetition": repetition,
                                "split": phase,
                                "selection": "selected" if chosen else "rejected",
                                "selection_reason": "frozen_candidate_family"
                                if compatible
                                else "combined_scope_not_attributed"
                                if chosen
                                else "outside_candidate_family",
                                "synthetic": protocol["synthetic"],
                                "context": context,
                                "prediction": finding,
                                "prediction_recorded_at": journal["prediction_recorded_at"],
                                "outcome": {
                                    "record": (target / "intervention.json")
                                    .relative_to(out)
                                    .as_posix(),
                                    "baseline_trace": (folder / "baseline.json")
                                    .relative_to(out)
                                    .as_posix(),
                                    "candidate_trace": (target / "candidate.json")
                                    .relative_to(out)
                                    .as_posix(),
                                    "recorded_at": receipt["outcome_recorded_at"],
                                    "status": "completed",
                                }
                                if compatible
                                else None,
                            }
                        )
    corpus = FindingBenchmark(
        {
            "schema_version": "1.0",
            "name": "Real numeric non-agent workloads",
            "version": "1.0",
            "provenance_ref": "protocol:" + protocol["sha256"],
            "synthetic": protocol["synthetic"],
            "policy": {
                "id": "nonagent-independent-row-contract",
                "version": "1.0",
                "ref": "protocol:" + protocol["sha256"],
            },
            "cases": cases,
        }
    )
    write_new(out / "finding-corpus.json", corpus.to_dict())
    benchmark = run_finding_benchmark(
        corpus, split="evaluation", source_revision=protocol["source_revision"]
    )
    write_new(out / "finding-results.json", benchmark)
    (out / "finding-results.md").write_text(
        finding_benchmark_markdown(corpus, benchmark), encoding="utf-8"
    )
    now = datetime.now(timezone.utc)
    manifest = {
        "schema_version": "1.0",
        "name": "Non-agent numeric model original estimates",
        "as_of": now.isoformat(),
        "valid_until": (now + timedelta(days=7)).isoformat(),
        "fit_method": "none",
        "min_fit_tasks": 8,
        "selection_inventory_complete": complete_inventory,
        "bootstrap": protocol["bootstrap"],
        "registrations": registrations,
    }
    write_new(out / "calibration-manifest.json", manifest)
    write_new(out / "calibration-exclusions.json", exclusions)
    calibration = summarize_calibration(out / "calibration-manifest.json")
    write_new(out / "calibration-results.json", calibration)
    (out / "calibration-results.md").write_text(
        calibration_to_markdown(calibration), encoding="utf-8"
    )
    result = summarize_rows(protocol, rows, emission)
    result.update(
        finding_corpus_hash=corpus.corpus_hash,
        finding_summary=benchmark["summary"],
        calibration_artifact_hash=calibration["artifact_hash"],
        calibration_registration_count=len(registrations),
        calibration_cohort_count=len(calibration["cohorts"]),
    )
    result["sha256"] = digest(result)
    write_new(out / "results.json", result)
    (out / "REPORT.md").write_text(markdown(result), encoding="utf-8")
    return result


def summarize_rows(protocol, rows, emission):
    summaries = []
    for workload in protocol["workloads"]:
        for phase in ("fit", "held_out"):
            for variant in workload["variants"]:
                selected = [
                    row
                    for row in rows
                    if row["workload"] == workload["id"]
                    and row["split"] == phase
                    and row["variant"] == variant
                ]
                observed = [row for row in selected if row["availability"] == "recorded"]
                changes = {}
                for key in (
                    "runtime_ms_delta",
                    "unrecorded_runtime_delta_ms",
                    "quality_score_delta",
                ):
                    groups = defaultdict(list)
                    for row in observed:
                        value = (
                            row["unrecorded_runtime_delta_ms"]
                            if key == "unrecorded_runtime_delta_ms"
                            else row["deltas"].get(key)
                        )
                        if value is not None:
                            groups[row["task_id"]].append(value)
                    values = [statistics.fmean(group) for group in groups.values()]
                    changes[key] = {
                        "task_count": len(values),
                        "summary": summarize_values(values),
                        "interval": _bootstrap(values, protocol["bootstrap"]),
                    }
                summaries.append(
                    {
                        "workload": workload["id"],
                        "category": workload["category"],
                        "phase": phase,
                        "variant": variant,
                        "planned_pairs": len(selected),
                        "recorded_pairs": len(observed),
                        "owner_quality_passes": sum(
                            row["owner_quality_gate_passed"] for row in observed
                        ),
                        "quality_regressions": sum(
                            row["quality"]["candidate_score"] < row["quality"]["baseline_score"]
                            for row in observed
                        ),
                        "baseline_quality": summarize_values(
                            [row["quality"]["baseline_score"] for row in observed]
                        ),
                        "candidate_quality": summarize_values(
                            [row["quality"]["candidate_score"] for row in observed]
                        ),
                        "changes": changes,
                        "candidate_recording_overhead_ms": summarize_values(
                            [row["candidate_recording_overhead_ms"] for row in observed]
                        ),
                        "operating_cost_status": "unknown",
                        "language_tokens": "not_applicable_unavailable",
                        "paid_provider_spend_usd": 0,
                    }
                )
    counts = Counter(
        item["selection"]
        for baseline in emission
        for choices in baseline["findings"].values()
        for item in choices
    )
    return {
        "schema_version": "1.0",
        "protocol_sha256": protocol["sha256"],
        "source_revision": protocol["source_revision"],
        "synthetic": protocol["synthetic"],
        "data_kind": protocol["data_kind"],
        "workloads": summaries,
        "selection_counts": dict(counts),
        "baselines": emission,
        "owner_quality_approval": protocol["owner_quality_approval"],
        "accounting": protocol["accounting"],
        "limits": [
            protocol["limits"],
            "Instrumented and recording-disabled timings are reported separately; observer overhead can dominate these short CPU calls.",
            "Task-cluster intervals describe the fixed public-data groups; repetitions are not independent tasks. No quality criterion was tuned on held-out outcomes.",
            "Current routing heuristics see unavailable token placeholders on non-language models; those findings require investigation, not automatic acceptance.",
            "Raw record identifiers are omitted. Hash references are not an anonymity guarantee. These applications use numeric comparison/features, not raw personal names, images or customer traffic.",
        ],
    }


def markdown(result):
    def number(value):
        return "unavailable" if value is None else f"{value:.4f}"

    lines = [
        "# Actual non-agent workload validation",
        "",
        result["data_kind"],
        "",
        f"Protocol: {result['protocol_sha256']}",
        "",
        "| Workload | Phase / candidate | Recorded / planned | Owner quality passes | Quality regressions | Baseline / candidate quality | Instrumented delta (ms) | Unrecorded delta (ms) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in result["workloads"]:
        lines.append(
            f"| {row['workload']} | {row['phase']} / {row['variant']} | {row['recorded_pairs']}/{row['planned_pairs']} | {row['owner_quality_passes']}/{row['planned_pairs']} | {row['quality_regressions']} | {number(row['baseline_quality']['mean'])} / {number(row['candidate_quality']['mean'])} | {number(row['changes']['runtime_ms_delta']['summary']['mean'])} | {number(row['changes']['unrecorded_runtime_delta_ms']['summary']['mean'])} |"
        )
    lines += [
        "",
        "Negative runtime deltas mean faster candidates. Quality passes require the predeclared dataset-label score floor and paired non-regression. A faster low-quality result is not an accepted optimization.",
        "",
        "## Finding and estimator evidence",
        "",
        f"Selection inventory: {result['selection_counts']}. Native finding and calibration artifacts retain selected and rejected findings; compatible isolated batching estimates are compared with original measured outcomes. Combined candidates remain unattributed and no fitted factor is installed.",
        "",
        "## Scope and limits",
        "",
    ] + ["- " + value for value in result["limits"]]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--fit", type=Path, required=True)
    parser.add_argument("--held-out", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    result = export(args.plan, args.fit, args.held_out, args.out_dir)
    print(
        json.dumps(
            {
                "sha256": result["sha256"],
                "registration_count": result["calibration_registration_count"],
            }
        )
    )
