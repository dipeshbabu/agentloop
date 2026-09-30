"""Execute the frozen public-data study with predictions recorded before candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["OMP_NUM_THREADS"] = "1"

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agentloop import reset_runtime
from agentloop.findings import build_diagnosis
from agentloop.interventions import build_intervention
from agentloop.onboarding import validate_capture
from agentloop.replay import ReplayGates, build_replay_report
from examples.non_agent_study.data import digest, read, write_new
from examples.non_agent_study.models import Model
from examples.non_agent_study.protocol import ROOT
from examples.non_agent_study.workloads import quality_report, run_task


def load_plan(path, *, verify_sources=True):
    path = Path(path)
    protocol = read(path)
    value = dict(protocol)
    if value.pop("sha256") != digest(value):
        raise ValueError("study protocol hash mismatch")
    if verify_sources:
        for name, checksum in protocol["source_hashes"].items():
            target = (ROOT / name).resolve()
            if (
                not target.is_relative_to(ROOT.resolve())
                or hashlib.sha256(target.read_text(encoding="utf-8").encode()).hexdigest()
                != checksum
            ):
                raise ValueError("study source differs from frozen protocol")
    return protocol


def execute(protocol_path, out, *, phase):
    import numpy as np

    protocol_path, out = Path(protocol_path), Path(out)
    protocol = load_plan(protocol_path)
    if phase not in {"fit", "held_out"} or np.__version__ != protocol["numpy_version"]:
        raise ValueError("execution phase or NumPy version differs from protocol")
    reset_runtime()
    out.mkdir(parents=True, exist_ok=False)
    write_new(out / "protocol.json", protocol)
    environment = {
        "python": sys.version,
        "platform": platform.platform(),
        "numpy": np.__version__,
        "openblas_threads_requested": os.environ["OPENBLAS_NUM_THREADS"],
        "omp_threads_requested": os.environ["OMP_NUM_THREADS"],
        "phase": phase,
        "paid_provider_spend_usd": 0,
        "operating_cost_usd": None,
    }
    write_new(out / "environment.json", environment)
    rows = []
    for workload in protocol["workloads"]:
        began = perf_counter()
        models = {
            name: Model(read(protocol_path.parent / declaration["path"]))
            for name, declaration in workload["models"].items()
        }
        setup_ms = (perf_counter() - began) * 1000
        warmups = []
        for identity, model in models.items():
            for index in range(workload["warmup"]["repetitions"]):
                warm_started = perf_counter()
                output = model.predict([workload["warmup"]["input"]])
                warmups.append(
                    {
                        "model": identity,
                        "repetition": index,
                        "elapsed_ms": (perf_counter() - warm_started) * 1000,
                        "input_sha256": digest(workload["warmup"]["input"]),
                        "output_sha256": digest(output),
                    }
                )
        write_new(
            out / workload["id"] / "setup.json",
            {
                "model_load_ms": setup_ms,
                "model_hashes": {key: value.parameters["sha256"] for key, value in models.items()},
                "warmups": warmups,
                "setup_steps": [
                    "prepare licensed disjoint data",
                    "fit and freeze models/protocol",
                    "wrap host workflow and model boundaries",
                    "execute and inspect native evidence",
                ],
                "operator_minutes": None,
                "model_fitting_excluded_from_execution": True,
            },
        )
        for task in (item for item in workload["tasks"] if item["split"] == phase):
            for repetition in range(workload["repetitions"]):
                folder = out / workload["id"] / task["id"] / str(repetition)
                write_new(
                    folder / "started.json",
                    {
                        "protocol_sha256": protocol["sha256"],
                        "task_id": task["id"],
                        "repetition": repetition,
                        "started_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                options = {"repetition": repetition, "protocol_hash": protocol["sha256"]}
                if repetition % 2 == 0:
                    control = run_task(
                        workload, task, models, variant="baseline", recording=False, **options
                    )
                before = run_task(workload, task, models, variant="baseline", **options)
                if repetition % 2:
                    control = run_task(
                        workload, task, models, variant="baseline", recording=False, **options
                    )
                if before.metadata["output"] != control.metadata["output"]:
                    raise ValueError("recording altered baseline decisions")
                started = perf_counter()
                diagnosis = build_diagnosis(before)
                analysis_ms = (perf_counter() - started) * 1000
                selections = {
                    variant: [
                        {
                            "finding_id": finding["finding_id"],
                            "type": finding["type"],
                            "selection": "selected"
                            if finding["type"] == workload["selection"][variant]
                            else "rejected",
                            "reason": "preregistered_validation_only"
                            if finding["type"] == workload["selection"][variant]
                            else "outside_planned_candidate_scope",
                        }
                        for finding in diagnosis["findings"]
                    ]
                    for variant in workload["variants"]
                }
                write_new(folder / "baseline.json", before.to_dict())
                write_new(folder / "baseline-control.json", control.to_dict())
                write_new(folder / "prediction.json", diagnosis)
                write_new(folder / "selections.json", selections)
                prediction = {
                    "baseline_hash": digest(before.to_dict()),
                    "prediction_hash": digest(diagnosis),
                    "selection_hash": digest(selections),
                    "prediction_recorded_at": datetime.now(timezone.utc).isoformat(),
                    "analysis_ms": analysis_ms,
                    "validation": validate_capture(before),
                    "baseline_recording_overhead_ms": before.elapsed_ms - control.elapsed_ms,
                }
                write_new(folder / "prediction-journal.json", prediction)
                for variant in workload["variants"]:
                    started_at = datetime.now(timezone.utc).isoformat()
                    after = run_task(workload, task, models, variant=variant, **options)
                    after_control = run_task(
                        workload, task, models, variant=variant, recording=False, **options
                    )
                    if after.metadata["output"] != after_control.metadata["output"]:
                        raise ValueError("recording altered candidate decisions")
                    quality = quality_report(workload, task, before, after)
                    replay = build_replay_report(
                        before,
                        after,
                        quality_report=quality,
                        gates=ReplayGates(min_quality_score=workload["quality"]["minimum_score"]),
                    )
                    targets = [
                        row["finding_id"]
                        for row in selections[variant]
                        if row["selection"] == "selected"
                    ]
                    directory = folder / variant
                    configuration = {
                        "variant": variant,
                        "models": workload["models"],
                        "attribution": workload["attribution"][variant],
                        "protocol_hash": protocol["sha256"],
                    }
                    write_new(directory / "candidate.json", after.to_dict())
                    write_new(directory / "candidate-control.json", after_control.to_dict())
                    write_new(directory / "quality.json", quality)
                    write_new(directory / "replay.json", replay)
                    record = None
                    if targets:
                        record = build_intervention(
                            before,
                            after,
                            target_finding_ids=targets,
                            intervention_type="numeric_model_" + variant,
                            configuration=configuration,
                            diagnosis=diagnosis,
                            replay_report=replay,
                            metadata={
                                "synthetic": protocol["synthetic"],
                                "workload": workload["id"],
                                "task_id": task["id"],
                                "repetition": repetition,
                                "prediction_journal": prediction,
                                "candidate_started_at": started_at,
                            },
                        )
                        write_new(directory / "intervention.json", record.to_dict())
                    baseline_score, candidate_score = (
                        quality["baseline_score"],
                        quality["candidate_score"],
                    )
                    quality_ok = (
                        candidate_score is not None
                        and baseline_score is not None
                        and candidate_score >= workload["quality"]["minimum_score"]
                        and candidate_score >= baseline_score
                    )
                    result = {
                        "workload": workload["id"],
                        "category": workload["category"],
                        "task_id": task["id"],
                        "task_sha256": digest(task),
                        "split": phase,
                        "repetition": repetition,
                        "variant": variant,
                        "synthetic": protocol["synthetic"],
                        "protocol_hash": protocol["sha256"],
                        "prediction_journal": prediction,
                        "candidate_started_at": started_at,
                        "outcome_recorded_at": datetime.now(timezone.utc).isoformat(),
                        "intervention_id": record.intervention_id if record else None,
                        "baseline_hash": digest(before.to_dict()),
                        "candidate_hash": digest(after.to_dict()),
                        "quality": quality,
                        "owner_quality_gate_passed": quality_ok,
                        "native_gates": replay["gates"],
                        "deltas": replay["deltas"],
                        "unrecorded_runtime_delta_ms": after_control.elapsed_ms
                        - control.elapsed_ms,
                        "candidate_recording_overhead_ms": after.elapsed_ms
                        - after_control.elapsed_ms,
                        "operating_cost_usd": None,
                        "paid_provider_spend_usd": 0,
                        "attribution": workload["attribution"][variant],
                        "artifact_prefix": directory.relative_to(out).as_posix(),
                    }
                    result["sha256"] = digest(result)
                    write_new(directory / "receipt.json", result)
                    rows.append(result)
        print(workload["id"], phase, "complete", flush=True)
    summary = {
        "schema_version": "1.0",
        "phase": phase,
        "protocol_hash": protocol["sha256"],
        "rows": rows,
    }
    summary["sha256"] = digest(summary)
    write_new(out / "outcomes.json", summary)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("protocol", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--phase", choices=("fit", "held_out"), required=True)
    args = parser.parse_args()
    result = execute(args.protocol, args.out_dir, phase=args.phase)
    print(
        json.dumps(
            {
                "phase": result["phase"],
                "pairs": len(result["rows"]),
                "owner_quality_passes": sum(
                    row["owner_quality_gate_passed"] for row in result["rows"]
                ),
            }
        )
    )
