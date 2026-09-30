"""Deterministic synthetic finding-linked good/bad candidate experiment."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agentloop.entrypoint import _quickstart_trace
from agentloop.experiment_reports import export_experiment
from agentloop.experiment_types import (
    ExperimentBudget,
    ExperimentCase,
    ExperimentPlan,
    ExperimentResult,
    ExperimentRunner,
)
from agentloop.experiments import ExperimentSession
from agentloop.findings import build_diagnosis
from agentloop.replay import ReplayGates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/optimization-experiment"))
    out = parser.parse_args().out
    baseline = _quickstart_trace()
    baseline.metadata["output"] = 4
    targets = [
        item["finding_id"]
        for item in build_diagnosis(baseline)["findings"]
        if item["type"] == "parallelize_tools"
    ]
    case = ExperimentCase(
        "arithmetic",
        baseline,
        target_finding_ids=targets,
        inputs={"left": 2, "right": 2},
        expected=4,
        baseline_output=4,
        input_ref="synthetic-arithmetic:v1",
        quality_ref="independent-fixture:v1",
        baseline_implementation_ref="synthetic-baseline:v1",
        baseline_configuration_ref="fixture:v1",
    )
    runners = [
        ExperimentRunner(
            "correct",
            "1",
            "local-addition:v1",
            "fixture:v1",
            lambda request: ExperimentResult(request.inputs["left"] + request.inputs["right"]),
        ),
        ExperimentRunner(
            "regressing",
            "1",
            "local-regression:v1",
            "fixture:v1",
            lambda request: ExperimentResult(5),
        ),
    ]
    if (out / "experiment.json").exists():
        session = ExperimentSession.resume(out, cases=[case], runners=runners)
    else:
        plan = ExperimentPlan(
            "synthetic-candidate-comparison",
            cases=[case],
            runners=runners,
            intervention_type="caller-defined-change",
            intervention_version="1",
            scorer={"type": "exact_match", "version": "1.0"},
            gate_version="1",
            gates=ReplayGates(min_quality_score=1),
            budget=ExperimentBudget(2, 5),
            permission_ref="synthetic-fixtures-only",
            synthetic=True,
        )
        session = ExperimentSession(plan, cases=[case], runners=runners, root=out)
    execution = session.run(enabled=True)
    exported = export_experiment(out)
    print(
        json.dumps(
            {
                "synthetic": True,
                "executed_this_time": execution["executed"],
                "comparisons": exported["experiment"]["comparisons"],
                "evidence_directory": exported["directory"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
