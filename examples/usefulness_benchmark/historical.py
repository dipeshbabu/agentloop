"""Retrospective current-version analysis without rewriting original observations."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from time import perf_counter

from agentloop.findings import build_diagnosis
from agentloop.interventions import InterventionRecord
from agentloop.tracer import AgentTrace
from examples.usefulness_benchmark.protocol import fingerprint, read, write_new
from examples.usefulness_benchmark.reference import corpus_case, evaluate_corpus


def historical_fingerprint(value):
    """The archived application receipts used UTF-8 JSON; native ledgers use ASCII escapes."""
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def _analysis(trace):
    started = perf_counter()
    try:
        diagnosis = build_diagnosis(trace)
        status = "complete" if diagnosis.get("analysis_complete") else "partial"
    except Exception as exc:
        diagnosis = {
            "run_id": trace.run_id,
            "findings": [],
            "analysis_complete": False,
            "error_type": type(exc).__name__,
        }
        status = "error"
    return diagnosis, (perf_counter() - started) * 1000, status


def analyze_history(protocol, real_root, calibration_root, out):
    real_root, calibration_root = Path(real_root), Path(calibration_root)
    cases, rows, inventory = [], [], []
    original_protocols = {}
    for phase in ("pilot", "evaluation"):
        folder = real_root / "study" / (phase + "-report")
        summary = read(folder / "summary.json")
        original_protocols[phase] = read(real_root / "study" / (phase + "-frozen.json"))
        for source in summary["rows"]:
            workload, task, repeat = source["workload"], source["task_id"], source["repetition"]
            if (
                workload not in protocol["historical"]["workloads"]
                or not re.fullmatch(r"[a-z]+-[0-9]+", task)
                or type(repeat) is not int
                or not 0 <= repeat <= 5
                or not re.fullmatch(r"int_[0-9a-f]{64}", source["intervention_id"])
            ):
                raise ValueError("unexpected historical workload")
            stem = f"{task}-{repeat}"
            before = AgentTrace.from_json(folder / workload / "baseline" / (stem + ".json"))
            after = AgentTrace.from_json(folder / workload / "candidate" / (stem + ".json"))
            ledger_path = folder / "interventions" / (source["intervention_id"] + ".json")
            ledger = InterventionRecord.from_dict(read(ledger_path)).to_dict()
            if ledger["trace_fingerprints"] != {
                "baseline": fingerprint(before.to_dict()),
                "candidate": fingerprint(after.to_dict()),
            }:
                raise ValueError("historical trace/ledger binding mismatch")
            original_folder = (
                real_root / "study/runs" / phase / "baseline" / f"rep-{repeat}-traced" / task
            )
            original = read(original_folder / "diagnosis.json")
            original_receipt = read(original_folder / "receipt.json")
            if (
                historical_fingerprint(original) != original_receipt["diagnosis_hash"]
                or historical_fingerprint(before.to_dict()) != original_receipt["trace_hash"]
            ):
                raise ValueError("historical prediction/receipt binding mismatch")
            current, analysis_ms, status = _analysis(before)
            target = out / "historical-analysis" / phase / workload / stem
            write_new(target / "current-diagnosis.json", current)
            inventory.append(
                {
                    "workload": workload,
                    "task_id": task,
                    "repetition": repeat,
                    "phase": phase,
                    "analysis_ms": analysis_ms,
                    "analysis_status": status,
                    "current_findings": [
                        {
                            "finding_id": finding["finding_id"],
                            "type": finding["type"],
                            "status": "retrospective_not_tested",
                        }
                        for finding in current["findings"]
                    ],
                    "original_findings": original["findings"],
                    "original_selections": [
                        item
                        for item in summary["finding_selections"]
                        if item["task_id"] == task and item["repetition"] == repeat
                    ],
                    "source_prediction_sha256": fingerprint(original),
                    "current_prediction_sha256": fingerprint(current),
                    "original_recorded_prediction_hash": original_receipt["diagnosis_hash"],
                    "original_recorded_trace_hash": original_receipt["trace_hash"],
                    "original_hash_encoding": "unescaped UTF-8 canonical JSON; native trace/ledger hashes use ASCII escapes",
                    "source_trace_sha256": fingerprint(before.to_dict()),
                    "token_status": before.report()["token_status"],
                    "cost_status": before.report()["cost_status"],
                }
            )
            if phase == "evaluation" and repeat == 0:
                for definition in protocol["historical"]["labels"]:
                    cases.append(
                        corpus_case(
                            before,
                            identity="real:" + stem + ":" + definition["rule_id"],
                            workload="real:" + workload,
                            split="evaluation",
                            definition=definition,
                            synthetic=False,
                        )
                    )
            measured = ledger["measured"]
            rows.append(
                {
                    "workload": workload,
                    "kind": "real_archived",
                    "phase": phase,
                    "task_id": task,
                    "repetition": repeat,
                    "variant": "smaller_model",
                    "split": phase,
                    "availability": source["availability"],
                    "baseline": measured["baseline"],
                    "candidate": measured["candidate"],
                    "quality": source["quality"],
                    "deltas": measured["deltas"],
                    "gates": measured["gates"],
                    "baseline_task_status": source["baseline"]["status"],
                    "candidate_task_status": source["candidate"]["status"],
                    "baseline_error_spans": sum(event.status == "error" for event in before.events),
                    "candidate_error_spans": sum(event.status == "error" for event in after.events),
                    "intervention_id": ledger["intervention_id"],
                    "original_intervention_sha256": fingerprint(ledger),
                    "prediction_version": "original archived version, not the current reanalysis",
                    "attribution": "combined_configuration_unattributed",
                    "operating_cost_usd": None,
                    "paid_provider_spend_usd": 0,
                }
            )
    retired = []
    retired_root = real_root / "onboarding-v1"
    for source_path in sorted(
        (retired_root / "runs/pilot/baseline/rep-0-traced").glob("*/receipt.json")
    ):
        receipt = read(source_path)
        if not re.fullmatch(r"[a-z]+-[0-9]+", receipt["task_id"]):
            raise ValueError("invalid retired task identity")
        trace = AgentTrace.from_json(source_path.parent / "trace.json")
        original = read(source_path.parent / "diagnosis.json")
        if (
            historical_fingerprint(trace.to_dict()) != receipt["trace_hash"]
            or historical_fingerprint(original) != receipt["diagnosis_hash"]
        ):
            raise ValueError("retired onboarding evidence hash mismatch")
        current, elapsed, status = _analysis(trace)
        write_new(
            out
            / "historical-analysis/retired-onboarding"
            / receipt["task_id"]
            / "current-diagnosis.json",
            current,
        )
        retired.append(
            {
                "workload": receipt["workload"],
                "task_id": receipt["task_id"],
                "availability": {"baseline": "recorded", "candidate": "not_executed"},
                "baseline_status": receipt["status"],
                "baseline_quality": receipt["quality"],
                "candidate_quality": None,
                "analysis_ms": elapsed,
                "analysis_status": status,
                "source_trace_sha256": receipt["trace_hash"],
                "original_prediction_sha256": receipt["diagnosis_hash"],
                "current_findings": [
                    {"finding_id": finding["finding_id"], "type": finding["type"]}
                    for finding in current["findings"]
                ],
            }
        )
    calibration = read(calibration_root / "calibration/report.json")
    calibration_snapshot = {
        key: calibration[key]
        for key in (
            "schema_version",
            "name",
            "registration_count",
            "cohorts",
            "selection_inventory_complete",
            "runtime_coefficients_changed",
            "interpretation",
            "artifact_hash",
        )
    }
    result = {
        "kind": "real_archived",
        "mode": protocol["historical"]["mode"],
        "rows": rows,
        "baselines": inventory,
        "retired_onboarding": {
            "source_disposition": read(retired_root / "disposition.json"),
            "rows": retired,
        },
        "recording_overhead": read(real_root / "study/overhead.json"),
        "finding_evaluation": evaluate_corpus(
            cases, synthetic=False, protocol=protocol, out=out, prefix="real-findings"
        ),
        "historical_calibration": calibration_snapshot,
        "historical_protocols": {
            key: {
                "sha256": fingerprint(value),
                "agentloop_version": value["agentloop_version"],
                "models": value["models"],
                "scorer_version": value["scorer_version"],
                "implementation_hashes": value["implementation_hashes"],
            }
            for key, value in original_protocols.items()
        },
        "limits": "Current findings are retrospective and not assigned historical intervention effects. Original predictions, failures, selections, protocols and native ledgers remain in source archives. Local operating cost is unknown, and paid-provider spend is separately zero.",
    }
    result["sha256"] = fingerprint(result)
    write_new(out / "historical-results.json", result)
    return result
