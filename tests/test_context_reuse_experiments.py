from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agentloop.budget_types import ResourceUsage
from agentloop.context_experiments import (
    CONTEXT_EXPERIMENT_KEY,
    ContextSelection,
    context_reduction_runner,
)
from agentloop.context_types import ContextTokenCount
from agentloop.entrypoint import _quickstart_trace
from agentloop.experiment_observations import (
    observation,
    record_experiment_observations,
)
from agentloop.experiment_reports import export_experiment, read_experiment, summarize_experiment
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

AS_OF = "2026-09-15T00:00:00Z"


def backend(function=None):
    return ExperimentRunner(
        "backend",
        "1",
        "fixture-backend:v1",
        "fixture-config:v1",
        function or (lambda request: ExperimentResult(request.inputs.get("answer", 0))),
    )


def case(name, payload, expected=4):
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
        inputs=payload,
        expected=expected,
        baseline_output=expected,
        input_ref="fixture-input:v1",
        quality_ref="independent-answer:v1",
        baseline_implementation_ref="fixture-baseline:v1",
        baseline_configuration_ref="fixture-config:v1",
    )


def execute(root, candidates, *, cases=None, maximum=100):
    cases = cases or [
        case("one", {"answer": 4, "noise": "private context that must not appear in metadata"})
    ]
    plan = ExperimentPlan(
        "template-fixture",
        cases=cases,
        runners=candidates,
        intervention_type="template-test",
        intervention_version="1",
        scorer={"type": "exact_match", "version": "1.0"},
        gate_version="1",
        gates=ReplayGates(min_quality_score=1),
        budget=ExperimentBudget(maximum, 5),
        permission_ref="synthetic-only",
    )
    session = ExperimentSession(plan, cases=cases, runners=candidates, root=root)
    session.run(enabled=True)
    return session, summarize_experiment(root)


def counter(payload):
    return ContextTokenCount(len(canonical(payload)), "tokenizer", "synthetic-char-v1")


def entry(key=(4,), output=4, **changes):
    values = dict(
        key_version="1",
        implementation_ref="fixture-backend:v1",
        implementation_version="1",
        configuration_ref="fixture-config:v1",
        data_ref="fixture-data:v1",
        invalidation_epoch="epoch1",
        source_ref="prior-output:v1",
        created_at="2026-09-01T00:00:00Z",
        expires_at="2026-10-01T00:00:00Z",
    )
    values.update(changes)
    return ReuseEntry(list(key), output, **values)


def reuse(entries, *, original=None, **changes):
    return result_reuse_runner(
        "reuse",
        original or backend(),
        entries=entries,
        key_paths=changes.pop("key_paths", [("answer",)]),
        key_version="1",
        data_ref="fixture-data:v1",
        invalidation_epoch="epoch1",
        as_of=AS_OF,
        **changes,
    )


def test_context_reduction_does_not_imply_quality_preservation(tmp_path):
    good = context_reduction_runner(
        "good",
        backend(),
        selections=[ContextSelection("noise", ("noise",), "segment:noisy")],
        token_counter=counter,
        counter_ref="synthetic-char-v1",
    )
    bad = context_reduction_runner(
        "bad",
        backend(),
        selections=[ContextSelection("answer", ("answer",), "segment:required")],
        token_counter=counter,
        counter_ref="synthetic-char-v1",
    )
    _, report = execute(tmp_path, [good, bad])
    by_name = {item["candidate_id"]: item for item in report["comparisons"]}
    assert by_name["good"]["quality_pass_count"] == 1
    assert by_name["bad"]["quality_pass_count"] == 0
    reduction = by_name["bad"]["observations"]["exact_token_reduction"]
    assert reduction["true_count"] == 1 and reduction["quality_preserving_true_count"] == 0
    assert by_name["good"]["predicted_effects"] and by_name["good"]["measured_pairs"]
    for row in read_experiment(tmp_path)["rows"]:
        metadata = row["trace"]["metadata"]
        assert "private context" not in json.dumps(metadata)
        assert metadata[CONTEXT_EXPERIMENT_KEY]["selections"][0]["source_hash"]


def test_protected_and_overlapping_context_paths_are_rejected():
    for paths, protected in [([("a",)], [("a", "b")]), ([("a",), ("a", "b")], [])]:
        with pytest.raises(ValueError, match="overlap"):
            context_reduction_runner(
                "bad",
                backend(),
                selections=[
                    ContextSelection(str(i), path, "fixture") for i, path in enumerate(paths)
                ],
                protected_paths=protected,
            )


def test_array_removal_uses_original_positions(tmp_path):
    seen = []
    original = backend(
        lambda request: seen.append(request.inputs) or ExperimentResult(request.inputs["values"])
    )
    candidate = context_reduction_runner(
        "remove",
        original,
        selections=[
            ContextSelection("one", ("values", 1), "one"),
            ContextSelection("two", ("values", 2), "two"),
        ],
    )
    execute(tmp_path, [candidate], cases=[case("list", {"values": [0, 1, 2, 3]}, expected=[0, 3])])
    assert seen == [{"values": [0, 3]}]


@pytest.mark.parametrize("policy,expected", [("error", "failed"), ("keep_original", "completed")])
def test_missing_context_selection_is_retained(tmp_path, policy, expected):
    candidate = context_reduction_runner(
        "missing",
        backend(),
        selections=[ContextSelection("absent", ("absent",), "absent")],
        on_missing=policy,
    )
    _, report = execute(tmp_path, [candidate])
    assert report["status_counts"] == {expected: 1}
    assert report["comparisons"][0]["observations"]["context_applied"]["true_count"] == 0


def test_estimated_token_reduction_is_not_exact(tmp_path):
    candidate = context_reduction_runner(
        "estimated",
        backend(),
        selections=[ContextSelection("noise", ("noise",), "noise")],
        token_counter=lambda payload: ContextTokenCount(
            len(canonical(payload)), "estimated_words", "estimate-v1"
        ),
        counter_ref="estimate-v1",
    )
    _, report = execute(tmp_path, [candidate])
    metrics = report["comparisons"][0]["observations"]
    assert metrics["input_tokens_removed"]["kinds"] == ["estimated"]
    assert metrics["exact_token_reduction"]["missing_count"] == 1


def test_compressor_work_is_included_in_runner_usage(tmp_path):
    def usage(tokens):
        return ResourceUsage(tokens=tokens, token_provenance="user_supplied", complete=True)

    original = backend(lambda request: ExperimentResult(4, usage=usage(3)))
    candidate = context_reduction_runner(
        "compress",
        original,
        selections=[ContextSelection("noise", ("noise",), "noise", "compress")],
        compressor=lambda value, request: ExperimentResult("short", usage=usage(2)),
        compressor_ref="compressor:v1",
    )
    _, report = execute(tmp_path, [candidate])
    assert report["attempts"][0]["receipt"]["usage"]["tokens"] == 5
    assert report["budget"]["reported_total_cost_usd"] is None


@pytest.mark.parametrize(
    "state,changes",
    [
        ("stale", {"expires_at": "2026-09-02T00:00:00Z"}),
        ("invalidated", {"configuration_ref": "old"}),
        ("unknown_evidence", {"source_ref": None}),
        ("future_entry", {"created_at": "2026-09-16T00:00:00Z"}),
        ("invalid_lifetime", {"expires_at": "2026-08-01T00:00:00Z"}),
    ],
)
def test_unusable_reuse_entries_fall_back_without_counting_a_hit(tmp_path, state, changes):
    calls = []
    candidate = reuse(
        [entry(**changes)],
        original=backend(lambda request: calls.append(True) or ExperimentResult(4)),
    )
    _, report = execute(tmp_path, [candidate])
    observations = report["comparisons"][0]["observations"]
    assert observations["cache_hit"]["true_count"] == 0
    assert observations["cache_state"]["value_counts"] == {state: 1}
    assert calls == [True]


def test_reuse_hit_is_separate_from_independent_correctness(tmp_path):
    candidate = reuse(
        [entry(output=5)], original=backend(lambda request: pytest.fail("hit recomputed"))
    )
    _, report = execute(tmp_path, [candidate])
    comparison = report["comparisons"][0]
    assert comparison["quality_pass_count"] == 0
    assert comparison["observations"]["cache_hit"]["true_count"] == 1
    assert comparison["observations"]["cache_hit"]["quality_preserving_true_count"] == 0
    assert report["budget"]["reported_total_tokens"] == 0
    assert report["budget"]["reported_total_cost_usd"] is None


def test_incomplete_declared_key_can_match_wrong_task_and_fail_quality(tmp_path):
    candidate = reuse([entry(key=(2,), output=4)], key_paths=[("left",)])
    _, report = execute(
        tmp_path,
        [candidate],
        cases=[
            case("first", {"left": 2, "right": 2}, 4),
            case("second", {"left": 2, "right": 3}, 5),
        ],
    )
    observation = report["comparisons"][0]["observations"]["cache_hit"]
    assert observation["true_count"] == 2 and observation["quality_preserving_true_count"] == 1


def test_missing_key_and_ambiguous_entries_are_retained(tmp_path):
    ambiguous = reuse([entry(output=4), entry(output=5)])
    _, report = execute(tmp_path / "ambiguous", [ambiguous])
    assert report["comparisons"][0]["observations"]["cache_state"]["value_counts"] == {
        "ambiguous": 1
    }
    missing = reuse([entry()], key_paths=[("missing",)], on_unavailable="error")
    _, report = execute(tmp_path / "missing", [missing])
    assert report["status_counts"] == {"failed": 1}
    assert report["comparisons"][0]["observations"]["cache_state"]["value_counts"] == {
        "key_unavailable": 1
    }


def test_model_version_change_invalidates_entry(tmp_path):
    candidate = reuse([entry()], original=replace(backend(), version="2"))
    _, report = execute(tmp_path, [candidate])
    assert report["comparisons"][0]["observations"]["cache_state"]["value_counts"] == {
        "invalidated": 1
    }


def test_frozen_cache_resume_and_missing_slots_preserve_hit_denominator(tmp_path):
    candidate = reuse([entry()])
    session, report = execute(
        tmp_path, [candidate], cases=[case("a", {"answer": 4}), case("b", {"answer": 4})], maximum=1
    )
    assert session.run(enabled=True)["executed"] == 0
    hit = report["comparisons"][0]["observations"]["cache_hit"]
    assert hit["known_count"] == 1 and hit["missing_count"] == 1
    assert hit["true_fraction_of_planned"] == 0.5
    assert export_experiment(tmp_path)["experiment"] == report


def test_metric_writer_rejects_rewriting_observations(tmp_path):
    def invoke(request):
        record_experiment_observations({"value": observation(1, source_ref="fixture")})
        record_experiment_observations({"value": observation(2, source_ref="fixture")})
        return ExperimentResult(4)

    _, report = execute(tmp_path, [backend(invoke)])
    assert report["status_counts"] == {"failed": 1}
    assert report["comparisons"][0]["observations"]["value"]["values"]["mean"] == 1


def test_reuse_snapshot_never_implicitly_fills_on_miss(tmp_path):
    calls = []
    candidate = reuse(
        [], original=backend(lambda request: calls.append(True) or ExperimentResult(4))
    )
    _, report = execute(
        tmp_path, [candidate], cases=[case("a", {"answer": 4}), case("b", {"answer": 4})]
    )
    assert calls == [True, True]
    assert report["comparisons"][0]["observations"]["cache_state"]["value_counts"] == {"miss": 2}


def test_invalidated_epoch_and_expiry_boundary_are_not_hits(tmp_path):
    for index, changes in enumerate(
        ({"invalidation_epoch": "old"}, {"expires_at": AS_OF}, {"implementation_version": None})
    ):
        _, report = execute(tmp_path / str(index), [reuse([entry(**changes)])])
        assert report["comparisons"][0]["observations"]["cache_hit"]["true_count"] == 0


def test_compression_can_reduce_tokens_and_destroy_quality(tmp_path):
    text = "required context " * 30
    original = backend(
        lambda request: ExperimentResult(4 if request.inputs.get("answer") == text else 0)
    )
    candidate = context_reduction_runner(
        "bad-compression",
        original,
        selections=[ContextSelection("required", ("answer",), "required", "compress")],
        compressor=lambda value, request: ExperimentResult(None),
        compressor_ref="lossy:v1",
        token_counter=counter,
        counter_ref="synthetic-char-v1",
    )
    _, report = execute(tmp_path, [candidate], cases=[case("loss", {"answer": text})])
    assert report["comparisons"][0]["quality_pass_count"] == 0
    assert report["comparisons"][0]["observations"]["context_applied"]["true_count"] == 1
    assert report["comparisons"][0]["observations"]["exact_token_reduction"]["true_count"] == 1


def test_compressor_cancellation_is_preserved(tmp_path):
    from asyncio import CancelledError

    error = CancelledError("private")

    def compress(value, request):
        raise error

    candidate = context_reduction_runner(
        "cancel",
        backend(),
        selections=[ContextSelection("noise", ("noise",), "noise", "compress")],
        compressor=compress,
        compressor_ref="cancel:v1",
    )
    with pytest.raises(CancelledError) as caught:
        execute(tmp_path, [candidate])
    assert caught.value is error
    assert summarize_experiment(tmp_path)["status_counts"] == {"cancelled": 1}


def test_unknown_token_counts_do_not_claim_reduction(tmp_path):
    candidate = context_reduction_runner(
        "unknown", backend(), selections=[ContextSelection("noise", ("noise",), "noise")]
    )
    _, report = execute(tmp_path, [candidate])
    assert report["comparisons"][0]["observations"]["exact_token_reduction"]["missing_count"] == 1


def test_empty_output_and_null_keys_are_distinct_from_missing(tmp_path):
    candidate = reuse(
        [entry(key=(None,), output=None)],
        original=backend(lambda request: pytest.fail("valid null hit")),
    )
    _, report = execute(
        tmp_path, [candidate], cases=[case("null", {"answer": None}, expected=None)]
    )
    assert report["comparisons"][0]["quality_pass_count"] == 1
    assert report["comparisons"][0]["observations"]["cache_hit"]["true_count"] == 1


def test_estimated_and_observed_metrics_are_not_silently_combined(tmp_path):
    def invoke(request):
        record_experiment_observations(
            {
                "tokens": observation(
                    10,
                    kind="observed" if request.case_id == "a" else "estimated",
                    unit="tokens",
                    source_ref="counter:v1",
                )
            }
        )
        return ExperimentResult(4)

    _, report = execute(tmp_path, [backend(invoke)], cases=[case("a", {}), case("b", {})])
    result = report["comparisons"][0]["observations"]["tokens"]
    assert result["aggregation_status"] == "unknown_or_mixed_basis"
    assert result["known_count"] == 2 and "values" not in result


def test_changed_backend_binding_cannot_reuse_old_evidence(tmp_path):
    original = backend(lambda request: pytest.fail("changed backend invoked"))
    candidate = reuse([entry()], original=original)
    object.__setattr__(original, "configuration_ref", "different")
    _, report = execute(tmp_path, [candidate])
    assert report["status_counts"] == {"failed": 1}


def test_protected_path_declarations_are_bounded_before_iteration():
    with pytest.raises(ValueError, match="bounded sequence"):
        context_reduction_runner(
            "bounded",
            backend(),
            selections=[ContextSelection("noise", ("noise",), "noise")],
            protected_paths=(("field",) for _ in range(2)),
        )
