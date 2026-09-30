"""Synthetic structural candidates with retained routing and batch failures."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from threading import Barrier

from agentloop.batch_experiments import BatchConstraints, batching_experiment_runner
from agentloop.budget_types import Reservation, ResourceUsage
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
from agentloop.scheduling_types import ToolCall
from agentloop.structural_experiments import (
    conditional_experiment_runner,
    parallel_experiment_runner,
    stage_removal_runner,
)
from agentloop.structural_types import BatchItemOutcome, BatchResult, StageReference


def result(value):
    return ExperimentResult(
        value, usage=ResourceUsage(tokens=0, token_provenance="user_supplied", complete=True)
    )


def make_case(name, inputs, expected):
    baseline = _quickstart_trace()
    baseline.run_id = "synthetic_baseline_" + name
    for event in baseline.events:
        event.run_id = baseline.run_id
    baseline.metadata["output"] = expected
    target = build_diagnosis(baseline)["findings"][0]["finding_id"]
    return ExperimentCase(
        name,
        baseline,
        target_finding_ids=[target],
        inputs=inputs,
        expected=expected,
        baseline_output=expected,
        input_ref="synthetic-input:v1",
        quality_ref="independent-answer:v1",
        baseline_implementation_ref="synthetic-baseline:v1",
        baseline_configuration_ref="fixture:v1",
    )


def run_group(root, name, cases, runners):
    if (root / "experiment.json").exists():
        session = ExperimentSession.resume(root, cases=cases, runners=runners)
    else:
        plan = ExperimentPlan(
            name,
            cases=cases,
            runners=runners,
            intervention_type="structural",
            intervention_version="1",
            scorer={"type": "exact_match", "version": "1.0"},
            gate_version="1",
            gates=ReplayGates(min_quality_score=1),
            budget=ExperimentBudget(16, 5),
            permission_ref="synthetic-fixtures-only",
            synthetic=True,
        )
        session = ExperimentSession(plan, cases=cases, runners=runners, root=root)
    executed = session.run(enabled=True)["executed"]
    exported = export_experiment(root)
    return {
        "executed": executed,
        "candidates": [
            {
                "name": item["candidate_id"],
                "planned": item["planned_pairs"],
                "quality_pass_count": item["quality_pass_count"],
                "all_gates_passed": item["all_planned_gates_passed"],
            }
            for item in exported["experiment"]["comparisons"]
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/structural-experiments"))
    root = parser.parse_args().out
    refs = [StageReference(name, "1", "synthetic-stage:" + name) for name in ("a", "b")]
    graph_args = dict(
        stages=refs,
        factory_ref="synthetic-factory:v1",
        assembler_ref="synthetic-sum:v1",
        why_ref="synthetic-finding:v1",
        reservation=Reservation(),
    )

    def calls(request):
        return [
            ToolCall(
                name,
                lambda context: result(2),
                depends_on=(),
                reads=(),
                writes=(),
                concurrent=True,
                effect="read_only",
            )
            for name in ("a", "b")
        ]

    def parallel_calls(request):
        ready = Barrier(2)

        def leaf(context):
            ready.wait(timeout=3)
            return result(2)

        return [
            ToolCall(
                name, leaf, depends_on=(), reads=(), writes=(), concurrent=True, effect="read_only"
            )
            for name in ("a", "b")
        ]

    def assemble(request, values):
        return result(values["a"] + values["b"])

    graphs = [
        stage_removal_runner("remove_required", calls, assemble, removed={"a": 0}, **graph_args),
        parallel_experiment_runner(
            "parallel", parallel_calls, assemble, max_concurrency=2, **graph_args
        ),
    ]
    full = ExperimentRunner("full", "1", "full:v1", "fixture:v1", lambda request: result(4))
    fallback = ExperimentRunner(
        "fallback", "1", "fallback:v1", "fixture:v1", lambda request: result(0)
    )
    conditional = conditional_experiment_runner(
        "conditional",
        full,
        fallback,
        lambda request: request.inputs["take"],
        stages=[StageReference("expensive", "1", "synthetic-expensive:v1")],
        predicate_ref="synthetic-predicate:v1",
        why_ref="synthetic-routing:v1",
        reservation=Reservation(),
    )

    def batch(request):
        return BatchResult(
            [BatchItemOutcome(item.item_id, item.inputs * 2) for item in request.items]
        )

    def partial(request):
        return BatchResult(
            [
                BatchItemOutcome(item.item_id, item.inputs * 2)
                if item.inputs != 2
                else BatchItemOutcome(item.item_id, status="failed", error_code="synthetic_failure")
                for item in request.items
            ]
        )

    batch_args = dict(
        stage=StageReference("batch", "1", "synthetic-stage:batch"),
        items_path=("items",),
        constraints=BatchConstraints(2, 2, 10000, "synthetic-provider:v1"),
        implementation_ref="synthetic-batch:v1",
        configuration_ref="fixture:v1",
        why_ref="synthetic-finding:v1",
    )
    batches = [
        batching_experiment_runner("batch", batch, **batch_args),
        batching_experiment_runner("partial", partial, **batch_args),
    ]
    report = {
        "synthetic": True,
        "graphs": run_group(
            root / "graphs", "synthetic-graphs", [make_case("graph", {}, 4)], graphs
        ),
        "conditional": run_group(
            root / "conditional",
            "synthetic-conditional",
            [
                make_case("take", {"take": True}, 4),
                make_case("miss", {"take": False}, 4),
                make_case("unknown", {"take": None}, 4),
            ],
            [conditional],
        ),
        "batching": run_group(
            root / "batching",
            "synthetic-batching",
            [make_case("batch", {"items": [1, 2, 3]}, [2, 4, 6])],
            batches,
        ),
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
