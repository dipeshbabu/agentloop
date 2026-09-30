"""Execute frozen reference cases, preserving predictions before candidates."""

from __future__ import annotations

import json
from contextlib import ExitStack
from datetime import datetime, timezone
from time import perf_counter
from unittest.mock import patch

from agentloop import AgentTrace, attach_quality_report, reset_runtime
from agentloop.finding_benchmark_types import FindingBenchmark
from agentloop.finding_benchmarks import run_finding_benchmark
from agentloop.findings import build_diagnosis
from agentloop.interventions import build_intervention
from agentloop.quality import build_quality_report
from agentloop.replay import ReplayGates, build_replay_report
from examples import reference_support
from examples.usefulness_benchmark.protocol import REGISTRY, canonical, fingerprint, write_new


def _run(name, fixture, variant, fixture_hash, *, recording=True):
    module, runner, _, _ = REGISTRY[name]
    inputs = json.loads(canonical(fixture["input"]))
    with ExitStack() as stack:
        if not recording:
            stack.enter_context(
                patch.object(reference_support, "record_operation", lambda *args, **kwargs: None)
            )
            if hasattr(module, "record_operation"):
                stack.enter_context(
                    patch.object(module, "record_operation", lambda *args, **kwargs: None)
                )
        if name == "batch":
            trace, output = getattr(module, runner)(
                inputs, variant, fixture["id"], fixture_hash, capture_prompts=recording
            )
            trace.metadata["output"] = output
        else:
            trace = getattr(module, runner)(inputs, variant, fixture["id"], fixture_hash)
    return trace


def _quality(name, fixture, before, after, fixture_hash):
    if name == "batch":
        return REGISTRY[name][0].quality_for_chunk(
            fixture["input"],
            before,
            after,
            before.metadata.get("output", {}),
            after.metadata.get("output", {}),
            fixture_hash,
        )
    return build_quality_report(
        [
            {
                "schema_version": "2.0",
                "id": fixture["id"],
                "input_ref": "sha256:" + fingerprint(fixture["input"]),
                "expected_ref": "fixture:" + fixture_hash + ":" + fixture["id"],
                "expected": fixture["expected"],
                "scorer": {"type": "fields", "version": "1.0", "allow_extra": False},
            }
        ],
        baseline_trace=before,
        candidate_trace=after,
        min_score=1,
    )


def corpus_case(trace, *, identity, workload, split, definition, synthetic):
    label = definition["label"]
    return {
        "id": identity,
        "workload_ref": workload,
        "group": workload,
        "split": split,
        "scenario": "synthetic_reference" if synthetic else "retrospective_real_agent",
        "rule_id": definition["rule_id"],
        "label": label,
        "label_ref": "usefulness-label-policy:1.0:" + workload,
        "label_version": "1.0",
        "rationale": definition["rationale"],
        "opportunities": [{"family": definition["rule_id"], "spans": definition["spans"]}]
        if label == "opportunity"
        else [],
        "required_evidence": ["semantic"]
        if definition["rule_id"] in {"semantic_redundancy", "context_relevance"}
        else ["timing", "tokens"],
        "trace": trace.to_dict(),
    }


def evaluate_corpus(cases, *, synthetic, protocol, out, prefix):
    corpus = FindingBenchmark(
        {
            "schema_version": "1.0",
            "name": prefix,
            "version": "1.0",
            "provenance_ref": "benchmark:" + protocol["sha256"],
            "synthetic": synthetic,
            "policy": {
                "id": "usefulness-label-policy",
                "version": "1.0",
                "ref": "benchmark:" + protocol["sha256"],
            },
            "cases": cases,
        }
    )
    write_new(out / (prefix + "-corpus.json"), corpus.to_dict())
    results = {}
    for split in sorted({case["split"] for case in cases}):
        result = run_finding_benchmark(
            corpus, split=split, source_revision=protocol["analyzer"]["source_revision"]
        )
        write_new(out / (prefix + "-" + split + ".json"), result)
        results[split] = result
    return {"corpus_sha256": corpus.corpus_hash, "results": results}


def execute_references(protocol, out):
    reset_runtime()
    cases, rows, baseline_inventory, overhead = [], [], [], []
    for workload in protocol["workloads"]:
        name = workload["id"]
        if name not in REGISTRY or workload["variants"] != list(REGISTRY[name][0].VARIANTS[1:]):
            raise ValueError("unsupported frozen workload/configuration")
        fixture_hash = fingerprint(workload["cases"])
        for fixture in workload["cases"]:
            for repeat in range(workload["repetitions"]):
                slot = fingerprint([name, fixture["id"], repeat])[:20]
                folder = out / "reference" / name / slot
                write_new(
                    folder / "started.json",
                    {
                        "task_id": fixture["id"],
                        "repetition": repeat,
                        "protocol_sha256": protocol["sha256"],
                        "started_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                # Alternating control order avoids always warming the recorded condition first.
                if repeat % 2 == 0:
                    control = _run(name, fixture, "baseline", fixture_hash, recording=False)
                before = _run(name, fixture, "baseline", fixture_hash)
                if repeat % 2:
                    control = _run(name, fixture, "baseline", fixture_hash, recording=False)
                if before.metadata.get("output") != control.metadata.get("output"):
                    raise ValueError("recording control changed reference task output")
                overhead.append(
                    {
                        "workload": name,
                        "task_id": fixture["id"],
                        "repetition": repeat,
                        "recorded_ms": before.elapsed_ms,
                        "control_ms": control.elapsed_ms,
                        "recording_delta_ms": before.elapsed_ms - control.elapsed_ms,
                        "scope": protocol["instrumentation_control"],
                    }
                )
                write_new(folder / "recording-control.json", control.to_dict())
                module, _, inspect_name, _ = REGISTRY[name]
                began = perf_counter()
                if inspect_name:
                    getattr(module, inspect_name)(before, fixture["input"], "baseline")
                inspection_ms = (perf_counter() - began) * 1000
                before.metadata.update(
                    benchmark_workload=name,
                    repetition=repeat,
                    benchmark_protocol=protocol["sha256"],
                )
                began = perf_counter()
                diagnosis = build_diagnosis(before)
                analysis_ms = (perf_counter() - began) * 1000
                write_new(folder / "baseline.json", before.to_dict())
                write_new(folder / "prediction.json", diagnosis)
                selection = [
                    {
                        "finding_id": finding["finding_id"],
                        "type": finding["type"],
                        "status": "selected"
                        if finding["type"] in workload["selection_families"]
                        else "rejected",
                        "reason": "Within the fixed combined candidate scope; no per-finding effect attribution."
                        if finding["type"] in workload["selection_families"]
                        else "Outside the fixed candidate scope; no realized benefit credited.",
                    }
                    for finding in diagnosis["findings"]
                ]
                write_new(folder / "selection.json", selection)
                frozen = {
                    "baseline_sha256": fingerprint(before.to_dict()),
                    "diagnosis_sha256": fingerprint(diagnosis),
                    "selection_sha256": fingerprint(selection),
                    "frozen_at": datetime.now(timezone.utc).isoformat(),
                }
                write_new(folder / "prediction-receipt.json", frozen)
                baseline_inventory.append(
                    {
                        "workload": name,
                        "task_id": fixture["id"],
                        "repetition": repeat,
                        "split": workload["split"],
                        "baseline_path": (folder / "baseline.json").relative_to(out).as_posix(),
                        "prediction_path": (folder / "prediction.json").relative_to(out).as_posix(),
                        "findings": selection,
                        "analysis_ms": analysis_ms,
                        "semantic_inspection_ms": inspection_ms,
                        "token_status": before.report()["token_status"],
                        "cost_status": before.report()["cost_status"],
                    }
                )
                if repeat == 0:
                    for definition in fixture["finding_labels"]:
                        cases.append(
                            corpus_case(
                                before,
                                identity=slot + ":" + definition["rule_id"],
                                workload=name,
                                split=workload["split"],
                                definition=definition,
                                synthetic=True,
                            )
                        )
                targets = [row["finding_id"] for row in selection if row["status"] == "selected"]
                for variant in workload["variants"]:
                    candidate_start = datetime.now(timezone.utc).isoformat()
                    after = _run(name, fixture, variant, fixture_hash)
                    after.metadata.update(
                        benchmark_workload=name,
                        repetition=repeat,
                        benchmark_protocol=protocol["sha256"],
                    )
                    quality = _quality(name, fixture, before, after, fixture_hash)
                    replay = build_replay_report(
                        before,
                        after,
                        quality_report=quality,
                        gates=ReplayGates(min_quality_score=1),
                    )
                    directory = folder / variant
                    write_new(directory / "candidate.json", after.to_dict())
                    write_new(directory / "quality.json", quality)
                    write_new(directory / "replay.json", replay)
                    record = None
                    if targets:
                        record = build_intervention(
                            before,
                            after,
                            target_finding_ids=targets,
                            intervention_type="reference_configuration_change",
                            configuration={
                                "variant": variant,
                                "protocol_sha256": protocol["sha256"],
                                "attribution": workload["attribution"],
                            },
                            metadata={
                                "synthetic": True,
                                "workload": name,
                                "task_id": fixture["id"],
                                "repetition": repeat,
                                "prediction_receipt": frozen,
                                "candidate_started_at": candidate_start,
                            },
                            diagnosis=diagnosis,
                            replay_report=replay,
                        )
                        write_new(directory / "intervention.json", record.to_dict())
                    for side, trace in (("baseline", before), ("candidate", after)):
                        owned = AgentTrace.from_dict(json.loads(canonical(trace.to_dict())))
                        attach_quality_report(owned, quality, side=side)
                        write_new(directory / (side + "-scored.json"), owned.to_dict())
                    row = {
                        "workload": name,
                        "kind": "synthetic_reference",
                        "task_id": fixture["id"],
                        "repetition": repeat,
                        "variant": variant,
                        "split": workload["split"],
                        "availability": {"baseline": "recorded", "candidate": "recorded"},
                        "baseline_sha256": frozen["baseline_sha256"],
                        "candidate_sha256": fingerprint(after.to_dict()),
                        "prediction_receipt": frozen,
                        "candidate_started_at": candidate_start,
                        "intervention_id": record.intervention_id if record else None,
                        "selection_status": "finding_linked_combined"
                        if record
                        else "unattributed_no_matching_finding",
                        "attribution": workload["attribution"],
                        "quality": {
                            key: quality.get(key)
                            for key in (
                                "baseline_score",
                                "candidate_score",
                                "passed",
                                "failed_case_count",
                            )
                        },
                        "baseline": replay["baseline"],
                        "candidate": replay["candidate"],
                        "deltas": replay["deltas"],
                        "gates": replay["gates"],
                        "baseline_error_spans": sum(
                            event.status == "error" for event in before.events
                        ),
                        "candidate_error_spans": sum(
                            event.status == "error" for event in after.events
                        ),
                        "artifact_prefix": directory.relative_to(out).as_posix(),
                    }
                    write_new(directory / "receipt.json", row)
                    rows.append(row)
    evaluation = evaluate_corpus(
        cases, synthetic=True, protocol=protocol, out=out, prefix="reference-findings"
    )
    result = {
        "kind": "synthetic_reference",
        "rows": rows,
        "baselines": baseline_inventory,
        "recording_overhead": overhead,
        "finding_evaluation": evaluation,
        "limits": "Fixture backend tokens/billing and judgments are synthetic. Combined configuration outcomes are not per-finding causal calibration.",
    }
    result["sha256"] = fingerprint(result)
    write_new(out / "reference-results.json", result)
    return result
