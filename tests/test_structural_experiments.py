from __future__ import annotations

from threading import Barrier

import pytest

from agentloop.batch_experiments import BatchConstraints, batching_experiment_runner
from agentloop.budget_types import Reservation, ResourceUsage
from agentloop.context_types import ContextTokenCount
from agentloop.entrypoint import _quickstart_trace
from agentloop.experiment_reports import read_experiment, summarize_experiment
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
from agentloop.structural_types import STRUCTURAL_KEY, BatchItemOutcome, BatchResult, StageReference


def known(output, tokens=0):
    return ExperimentResult(
        output, usage=ResourceUsage(tokens=tokens, token_provenance="user_supplied", complete=True)
    )


def case(name="one", inputs=None, expected=4):
    trace = _quickstart_trace()
    trace.run_id = "baseline_" + name
    for event in trace.events:
        event.run_id = trace.run_id
    trace.metadata["output"] = expected
    target = build_diagnosis(trace)["findings"][0]["finding_id"]
    return ExperimentCase(
        name,
        trace,
        target_finding_ids=[target],
        inputs={} if inputs is None else inputs,
        expected=expected,
        baseline_output=expected,
        input_ref="fixture:v1",
        quality_ref="answer:v1",
        baseline_implementation_ref="baseline:v1",
        baseline_configuration_ref="config:v1",
    )


def execute(root, runner, cases=None):
    cases = cases or [case()]
    plan = ExperimentPlan(
        "structural-fixture",
        cases=cases,
        runners=[runner],
        intervention_type="structural",
        intervention_version="1",
        scorer={"type": "exact_match", "version": "1.0"},
        gate_version="1",
        gates=ReplayGates(min_quality_score=1),
        budget=ExperimentBudget(100, 5),
        permission_ref="synthetic-only",
    )
    session = ExperimentSession(plan, cases=cases, runners=[runner], root=root)
    session.run(enabled=True)
    return session, summarize_experiment(root)


def call(name, function, dependencies=(), **changes):
    return ToolCall(
        name,
        function,
        depends_on=dependencies,
        reads=changes.pop("reads", ()),
        writes=changes.pop("writes", ()),
        concurrent=changes.pop("concurrent", True),
        effect=changes.pop("effect", "read_only"),
        **changes,
    )


def graph_args(*names):
    return {
        "stages": [StageReference(name, "1", "source:" + name) for name in names],
        "factory_ref": "factory:v1",
        "assembler_ref": "assembler:v1",
        "why_ref": "finding:fixture",
        "reservation": Reservation(),
    }


def test_removing_required_stage_preserves_downstream_quality_failure(tmp_path):
    visited = []

    def factory(request):
        return [
            call("required", lambda context: visited.append("required") or known(4)),
            call("answer", lambda context: known(context.prerequisites["required"]), ("required",)),
        ]

    candidate = stage_removal_runner(
        "remove",
        factory,
        lambda request, outputs: known(outputs["answer"]),
        removed={"required": 0},
        **graph_args("required", "answer"),
    )
    _, report = execute(tmp_path, candidate)
    assert not visited and report["comparisons"][0]["quality_pass_count"] == 0
    trace = read_experiment(tmp_path)["rows"][0]["trace"]
    evidence = trace["metadata"][STRUCTURAL_KEY]
    assert evidence["skipped_stages"] == ["required"]
    assert evidence["invoked_stages"] == ["answer"]
    assert not any(event["name"] == "required" for event in trace["events"])


def test_removing_unused_stage_preserves_quality(tmp_path):
    candidate = stage_removal_runner(
        "remove",
        lambda request: [
            call("unused", lambda context: pytest.fail("removed work")),
            call("answer", lambda context: known(4)),
        ],
        lambda request, outputs: known(outputs["answer"]),
        removed={"unused": None},
        **graph_args("unused", "answer"),
    )
    _, report = execute(tmp_path, candidate)
    assert report["comparisons"][0]["all_planned_gates_passed"]


def test_parallel_candidate_requires_actual_declared_independence(tmp_path):
    barrier = Barrier(2)

    def leaf(context):
        barrier.wait(timeout=3)
        return known(2)

    candidate = parallel_experiment_runner(
        "parallel",
        lambda request: [call("a", leaf), call("b", leaf)],
        lambda request, outputs: known(outputs["a"] + outputs["b"]),
        max_concurrency=2,
        **graph_args("a", "b"),
    )
    _, report = execute(tmp_path, candidate)
    metrics = report["comparisons"][0]["observations"]
    assert metrics["peak_parallel_stages"]["values"]["mean"] == 2
    assert metrics["parallel_execution_observed"]["true_count"] == 1
    assert report["comparisons"][0]["quality_pass_count"] == 1


@pytest.mark.parametrize("trap", ["unknown", "cycle", "missing", "same_names"])
def test_unsafe_dependency_traps_do_not_dispatch(tmp_path, trap):
    visited = []

    def factory(request):
        def work(context):
            visited.append(True)
            return known(2)

        if trap == "unknown":
            return [ToolCall("a", work), call("b", work)]
        if trap == "cycle":
            return [call("a", work, ("b",)), call("b", work, ("a",))]
        if trap == "missing":
            return [call("a", work, ("absent",)), call("b", work)]
        return [ToolCall("a", work), ToolCall("b", work)]

    candidate = parallel_experiment_runner(
        "trap", factory, lambda request, outputs: known(4), **graph_args("a", "b")
    )
    _, report = execute(tmp_path, candidate)
    assert not visited and report["status_counts"] == {"failed": 1}
    assert report["comparisons"][0]["observations"]["structural_state"]["value_counts"] == {
        "abstained": 1
    }


def test_shared_mutating_resource_is_serialized(tmp_path):
    order = []

    def work(context):
        order.append(context.call_id)
        return known(2)

    candidate = parallel_experiment_runner(
        "conflict",
        lambda request: [
            call("a", work, writes=("shared",), effect="mutating"),
            call("b", work, writes=("shared",), effect="mutating"),
        ],
        lambda request, outputs: known(outputs["a"] + outputs["b"]),
        **graph_args("a", "b"),
    )
    _, report = execute(tmp_path, candidate)
    assert order == ["a", "b"]
    assert report["comparisons"][0]["observations"]["peak_parallel_stages"]["values"]["mean"] == 1


def test_conditional_miss_and_unknown_have_separate_quality_cohorts(tmp_path):
    expensive = ExperimentRunner(
        "expensive", "1", "expensive:v1", "config:v1", lambda request: known(4)
    )
    fallback = ExperimentRunner(
        "fallback", "1", "fallback:v1", "config:v1", lambda request: known(0)
    )
    candidate = conditional_experiment_runner(
        "conditional",
        expensive,
        fallback,
        lambda request: request.inputs["decision"],
        stages=[StageReference("expensive-stage", "1", "source:stage")],
        predicate_ref="predicate:v1",
        why_ref="uncertainty:v1",
        reservation=Reservation(),
    )
    _, report = execute(
        tmp_path,
        candidate,
        [
            case("take", {"decision": True}),
            case("miss", {"decision": False}),
            case("unknown", {"decision": None}),
        ],
    )
    metrics = report["comparisons"][0]["observations"]
    assert metrics["conditional_predicate_known"]["true_count"] == 2
    assert metrics["conditional_route"]["value_counts"] == {"expensive": 2, "fallback": 1}
    assert metrics["conditional_route"]["quality_fraction_by_value"] == {
        "expensive": 1,
        "fallback": 0,
    }


def batching(function, **changes):
    return batching_experiment_runner(
        "batch",
        function,
        stage=StageReference("lookup", "1", "source:lookup"),
        items_path=("items",),
        constraints=changes.pop("constraints", BatchConstraints(2, 2, 10000, "provider:fixture")),
        implementation_ref="batch:v1",
        configuration_ref="config:v1",
        why_ref="finding:batch",
        **changes,
    )


def test_batch_outputs_are_reordered_by_item_id_and_throughput_is_end_to_end(tmp_path):
    def invoke(request):
        return BatchResult(
            [BatchItemOutcome(item.item_id, item.inputs * 2) for item in reversed(request.items)]
        )

    _, report = execute(
        tmp_path, batching(invoke), [case("batch", {"items": [1, 2, 3]}, [2, 4, 6])]
    )
    metrics = report["comparisons"][0]["observations"]
    assert metrics["batch_invocations"]["values"]["mean"] == 2
    rate = metrics["batch_items_completed"]["per_second_of_end_to_end_candidate_runtime"]["mean"]
    elapsed = read_experiment(tmp_path)["rows"][0]["intervention"]["measured"]["candidate"][
        "runtime_ms"
    ]
    assert rate == pytest.approx(3000 / elapsed)
    assert report["comparisons"][0]["quality_pass_count"] == 1


def test_partial_batch_failure_retains_completed_failed_and_unstarted_items(tmp_path):
    def invoke(request):
        return BatchResult(
            [
                BatchItemOutcome(request.items[0].item_id, 2),
                BatchItemOutcome(
                    request.items[1].item_id, status="failed", error_code="provider_rejected"
                ),
            ]
        )

    _, report = execute(
        tmp_path, batching(invoke), [case("partial", {"items": [1, 2, 3]}, [2, 4, 6])]
    )
    metrics = report["comparisons"][0]["observations"]
    assert report["status_counts"] == {"failed": 1}
    assert metrics["batch_items_completed"]["values"]["mean"] == 1
    assert metrics["batch_items_failed"]["values"]["mean"] == 1
    assert metrics["batch_items_not_started"]["values"]["mean"] == 1


@pytest.mark.parametrize("trap", ["payload", "token", "missing_result", "duplicate_result"])
def test_provider_constraints_and_invalid_batch_results_are_explicit(tmp_path, trap):
    calls = []

    def invoke(request):
        calls.append(True)
        if trap == "missing_result":
            return BatchResult([])
        return BatchResult([BatchItemOutcome(request.items[0].item_id, 2)] * 2)

    arguments = {}
    if trap == "payload":
        arguments["constraints"] = BatchConstraints(2, 2, 1, "provider:fixture")
    elif trap == "token":
        arguments.update(
            constraints=BatchConstraints(
                2, 2, 10000, "provider:fixture", provider_max_input_tokens=10
            ),
            token_counter=lambda items: ContextTokenCount(2, "estimated_words", "counter:v1"),
            counter_ref="counter:v1",
        )
    _, report = execute(
        tmp_path, batching(invoke, **arguments), [case("bad", {"items": [1, 2]}, [2, 4])]
    )
    assert report["status_counts"] == {"failed": 1}
    assert calls == ([] if trap in {"payload", "token"} else [True])


def test_batch_unknown_outcomes_are_not_success_or_silently_failed(tmp_path):
    def invoke(request):
        return BatchResult(
            [BatchItemOutcome(item.item_id, status="unknown") for item in request.items]
        )

    _, report = execute(tmp_path, batching(invoke), [case("unknown", {"items": [1, 2]}, [2, 4])])
    metrics = report["comparisons"][0]["observations"]
    assert metrics["batch_items_unknown"]["values"]["mean"] == 2
    assert metrics["batch_items_failed"]["values"]["mean"] == 0
    assert report["status_counts"] == {"failed": 1}


def test_explicit_partial_batch_policy_keeps_failed_positions(tmp_path):
    def invoke(request):
        return BatchResult(
            [
                BatchItemOutcome(item.item_id, item.inputs * 2)
                if item.inputs != 2
                else BatchItemOutcome(item.item_id, status="failed", error_code="fixture")
                for item in request.items
            ]
        )

    _, report = execute(
        tmp_path,
        batching(invoke, on_item_failure="return_partial"),
        [case("partial", {"items": [1, 2, 3]}, [2, 4, 6])],
    )
    assert report["status_counts"] == {"completed": 1}
    assert report["comparisons"][0]["quality_pass_count"] == 0
    assert report["comparisons"][0]["observations"]["batch_items_completed"]["values"]["mean"] == 2


def test_cancelled_stage_propagates_without_repeating_completed_work(tmp_path):
    from asyncio import CancelledError

    visited = []
    error = CancelledError("original")

    def cancel(context):
        raise error

    candidate = parallel_experiment_runner(
        "cancel",
        lambda request: [
            call("first", lambda context: visited.append(True) or known(2)),
            call("second", cancel, ("first",)),
        ],
        lambda request, outputs: known(4),
        **graph_args("first", "second"),
    )
    with pytest.raises(CancelledError) as caught:
        execute(tmp_path, candidate)
    assert caught.value is error and visited == [True]
    assert summarize_experiment(tmp_path)["status_counts"] == {"cancelled": 1}


def test_cancelled_batch_retains_unknown_item_outcomes(tmp_path):
    from asyncio import CancelledError

    error = CancelledError("original")

    def invoke(request):
        raise error

    with pytest.raises(CancelledError) as caught:
        execute(tmp_path, batching(invoke), [case("cancel", {"items": [1, 2, 3]}, [2, 4, 6])])
    assert caught.value is error
    report = summarize_experiment(tmp_path)
    assert report["status_counts"] == {"cancelled": 1}
    assert report["comparisons"][0]["observations"]["batch_items_unknown"]["values"]["mean"] == 2
    assert (
        report["comparisons"][0]["observations"]["batch_items_not_started"]["values"]["mean"] == 1
    )


@pytest.mark.parametrize("unknown", ["fallback", "error"])
def test_conditional_unknown_policy_is_explicit(tmp_path, unknown):
    expensive = ExperimentRunner("full", "1", "full:v1", "config:v1", lambda request: known(4))
    fallback = ExperimentRunner(
        "fallback", "1", "fallback:v1", "config:v1", lambda request: known(0)
    )
    candidate = conditional_experiment_runner(
        "unknown",
        expensive,
        fallback,
        lambda request: None,
        stages=[StageReference("stage", "1", "source")],
        predicate_ref="unknown:v1",
        why_ref="why",
        reservation=Reservation(),
        on_unknown=unknown,
    )
    _, report = execute(tmp_path, candidate)
    assert report["comparisons"][0]["quality_pass_count"] == 0
    assert report["comparisons"][0]["observations"]["conditional_route"]["value_counts"] == {
        unknown: 1
    }


def test_model_assisted_predicate_usage_is_counted(tmp_path):
    expensive = ExperimentRunner("full", "1", "full:v1", "config:v1", lambda request: known(4, 3))
    fallback = ExperimentRunner(
        "fallback", "1", "fallback:v1", "config:v1", lambda request: known(0, 1)
    )
    candidate = conditional_experiment_runner(
        "predicate",
        expensive,
        fallback,
        lambda request: known(True, 2),
        stages=[StageReference("stage", "1", "source")],
        predicate_ref="predicate:v1",
        why_ref="why",
        reservation=Reservation(),
    )
    _, report = execute(tmp_path, candidate)
    assert report["budget"]["reported_total_tokens"] == 5


def test_batch_count_limit_rejects_before_provider_invocation(tmp_path):
    candidate = batching(
        lambda request: pytest.fail("provider invoked"),
        constraints=BatchConstraints(1, 1, 10000, "provider", max_batches=1),
    )
    _, report = execute(tmp_path, candidate, [case("too-many", {"items": [1, 2]}, [2, 4])])
    assert report["status_counts"] == {"failed": 1}
    assert report["comparisons"][0]["observations"]["batch_invocations"]["values"]["mean"] == 0


def test_stage_removal_keeps_downstream_exception_and_original_plan(tmp_path):
    candidate = stage_removal_runner(
        "remove",
        lambda request: [
            call("prepare", lambda context: known(3)),
            call(
                "consume", lambda context: known(context.prerequisites["prepare"] + 1), ("prepare",)
            ),
        ],
        lambda request, outputs: known(outputs["consume"]),
        removed={"prepare": None},
        **graph_args("prepare", "consume"),
    )
    _, report = execute(tmp_path, candidate)
    assert report["status_counts"] == {"failed": 1}
    receipt = read_experiment(tmp_path)["rows"][0]["trace"]["metadata"][STRUCTURAL_KEY]
    assert receipt["original_plan"]["effective_dependencies"]["consume"] == ["prepare"]
    assert receipt["skipped_stages"] == ["prepare"]


def test_explicit_nonconcurrent_stage_remains_serial(tmp_path):
    candidate = parallel_experiment_runner(
        "serial",
        lambda request: [
            call("a", lambda context: known(2), concurrent=False),
            call("b", lambda context: known(2)),
        ],
        lambda request, outputs: known(outputs["a"] + outputs["b"]),
        **graph_args("a", "b"),
    )
    _, report = execute(tmp_path, candidate)
    assert (
        report["comparisons"][0]["observations"]["parallel_execution_observed"]["true_count"] == 0
    )


def test_batch_usage_is_preserved_when_item_mapping_is_invalid(tmp_path):
    usage = ResourceUsage(tokens=17, token_provenance="user_supplied", complete=True)
    candidate = batching(lambda request: BatchResult([], usage))
    execute(tmp_path, candidate, [case("bad-map", {"items": [1]}, [2])])
    receipt = read_experiment(tmp_path)["rows"][0]["trace"]["metadata"][STRUCTURAL_KEY]
    assert receipt["batches"][0]["usage"]["tokens"] == 17
    assert receipt["batches"][0]["outcomes"][0]["status"] == "unknown"
