"""Synthetic context/reuse candidates with independent quality regressions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agentloop.context_experiments import ContextSelection, context_reduction_runner
from agentloop.context_types import ContextTokenCount
from agentloop.entrypoint import _quickstart_trace
from agentloop.experiment_reports import export_experiment
from agentloop.experiment_types import (
    ExperimentBudget,
    ExperimentCase,
    ExperimentPlan,
    ExperimentResult,
    ExperimentRunner,
    canonical,
)
from agentloop.experiments import ExperimentSession
from agentloop.findings import build_diagnosis
from agentloop.replay import ReplayGates
from agentloop.reuse_experiments import ReuseEntry, result_reuse_runner


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/context-reuse-experiments"))
    out = parser.parse_args().out
    cases = []
    for value in (4, 5):
        baseline = _quickstart_trace()
        baseline.run_id = "synthetic_baseline_" + str(value)
        for event in baseline.events:
            event.run_id = baseline.run_id
        baseline.metadata["output"] = value
        target = build_diagnosis(baseline)["findings"][0]["finding_id"]
        cases.append(
            ExperimentCase(
                "case" + str(value),
                baseline,
                target_finding_ids=[target],
                inputs={
                    "answer": value,
                    "group": "same",
                    "noise": "synthetic optional context " * 20,
                },
                expected=value,
                baseline_output=value,
                input_ref="synthetic-input:v1",
                quality_ref="independent-answer:v1",
                baseline_implementation_ref="synthetic-baseline:v1",
                baseline_configuration_ref="fixture:v1",
            )
        )
    backend = ExperimentRunner(
        "backend",
        "1",
        "synthetic-backend:v1",
        "fixture:v1",
        lambda request: ExperimentResult(request.inputs.get("answer", 0)),
    )

    def count(payload):
        return ContextTokenCount(len(canonical(payload)), "tokenizer", "synthetic-char-v1")

    def entry(key, output):
        return ReuseEntry(
            key,
            output,
            key_version="1",
            implementation_ref=backend.implementation_ref,
            implementation_version=backend.version,
            configuration_ref=backend.configuration_ref,
            data_ref="synthetic-data:v1",
            invalidation_epoch="epoch1",
            source_ref="synthetic-prior-output:v1",
            created_at="2026-01-01T00:00:00Z",
            expires_at="2026-02-01T00:00:00Z",
        )

    runners = [
        context_reduction_runner(
            "remove_optional",
            backend,
            selections=[ContextSelection("optional", ("noise",), "synthetic-optional:v1")],
            token_counter=count,
            counter_ref="synthetic-char-v1",
        ),
        context_reduction_runner(
            "remove_required",
            backend,
            selections=[ContextSelection("required", ("answer",), "synthetic-required:v1")],
            token_counter=count,
            counter_ref="synthetic-char-v1",
        ),
        result_reuse_runner(
            "reuse_valid",
            backend,
            entries=[entry([4], 4), entry([5], 5)],
            key_paths=[("answer",)],
            key_version="1",
            data_ref="synthetic-data:v1",
            invalidation_epoch="epoch1",
            as_of="2026-01-15T00:00:00Z",
        ),
        result_reuse_runner(
            "reuse_incomplete_key",
            backend,
            entries=[entry(["same"], 4)],
            key_paths=[("group",)],
            key_version="1",
            data_ref="synthetic-data:v1",
            invalidation_epoch="epoch1",
            as_of="2026-01-15T00:00:00Z",
        ),
    ]
    if (out / "experiment.json").exists():
        session = ExperimentSession.resume(out, cases=cases, runners=runners)
    else:
        plan = ExperimentPlan(
            "synthetic-context-and-reuse",
            cases=cases,
            runners=runners,
            intervention_type="context-and-reuse",
            intervention_version="1",
            scorer={"type": "exact_match", "version": "1.0"},
            gate_version="1",
            gates=ReplayGates(min_quality_score=1),
            budget=ExperimentBudget(8, 5),
            permission_ref="synthetic-only",
            synthetic=True,
        )
        session = ExperimentSession(plan, cases=cases, runners=runners, root=out)
    execution = session.run(enabled=True)
    exported = export_experiment(out)
    print(
        json.dumps(
            {
                "synthetic": True,
                "executed": execution["executed"],
                "candidates": [
                    {
                        "candidate": item["candidate_id"],
                        "quality_pass_count": item["quality_pass_count"],
                        "planned_pairs": item["planned_pairs"],
                        "all_gates_passed": item["all_planned_gates_passed"],
                        "cache_hits": item["observations"].get("cache_hit"),
                    }
                    for item in exported["experiment"]["comparisons"]
                ],
                "evidence_directory": exported["directory"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
