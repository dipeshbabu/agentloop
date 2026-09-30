"""Recompute quality, native labels and calibration without fitting or inference."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agentloop.calibration import summarize_calibration
from agentloop.finding_benchmark_types import FindingBenchmark
from agentloop.finding_benchmarks import summarize_finding_benchmark
from examples.non_agent_study.data import digest, read, write_new
from examples.non_agent_study.export import markdown, summarize_rows, validate_pair


def portable_calibration_hash(value):
    return digest(
        {key: item for key, item in value.items() if key not in {"source_paths", "artifact_hash"}}
    )


def reconstruct(root):
    root = Path(root)
    protocol = read(root / "plan/protocol.json")
    original = dict(protocol)
    if original.pop("sha256") != digest(original):
        raise ValueError("protocol changed")
    rows, emission = [], []
    for workload in protocol["workloads"]:
        for task in workload["tasks"]:
            phase = task["split"]
            phase_root = root / phase
            for repetition in range(workload["repetitions"]):
                folder = phase_root / workload["id"] / task["id"] / str(repetition)
                journal = read(folder / "prediction-journal.json")
                selections = read(folder / "selections.json")
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
                    receipt = validate_pair(
                        phase_root, protocol, workload, task, repetition, variant
                    )
                    rows.append(
                        {**receipt, "availability": "recorded"}
                        if receipt
                        else {
                            "workload": workload["id"],
                            "task_id": task["id"],
                            "split": phase,
                            "variant": variant,
                            "repetition": repetition,
                            "availability": "missing",
                        }
                    )
    corpus = FindingBenchmark(read(root / "finding-corpus.json"))
    finding = read(root / "finding-results.json")
    summary = summarize_finding_benchmark(corpus, finding)
    if summary != finding["summary"]:
        raise ValueError("finding summary changed")
    original_calibration = read(root / "calibration-results.json")
    recomputed_calibration = summarize_calibration(root / "calibration-manifest.json")
    if portable_calibration_hash(original_calibration) != portable_calibration_hash(
        recomputed_calibration
    ):
        raise ValueError("calibration evidence/statistics changed")
    result = summarize_rows(protocol, rows, emission)
    result.update(
        finding_corpus_hash=corpus.corpus_hash,
        finding_summary=summary,
        calibration_artifact_hash=original_calibration["artifact_hash"],
        calibration_registration_count=original_calibration["registration_count"],
        calibration_cohort_count=len(original_calibration["cohorts"]),
    )
    result["sha256"] = digest(result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    args = parser.parse_args()
    result = reconstruct(args.root)
    write_new(args.json_out, result)
    with args.markdown_out.open("x", encoding="utf-8") as handle:
        handle.write(markdown(result))
    print(result["sha256"])
