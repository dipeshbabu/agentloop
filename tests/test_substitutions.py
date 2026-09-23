from __future__ import annotations

import copy

import pytest
from workflow_fixtures import pipeline

from agentloop import JudgeIdentity, JudgeUsage
from agentloop.substitution_evidence import read_trial_evidence
from agentloop.substitution_export import export_substitution_studies
from agentloop.substitution_reports import summarize_substitution_experiment
from agentloop.substitution_types import (
    TRIAL_KEY,
    DecisionImplementation,
    DecisionResult,
    DecisionStep,
    SubstitutionExample,
    SubstitutionProtocol,
)
from agentloop.substitutions import run_substitution_experiment
from agentloop.tracer import AgentTrace, current_trace, trace_agent

FREE = JudgeUsage(0, 0, 0, "reported", "reported")


def protocol(*, repetitions=1, timeout_s=None):
    step = DecisionStep.from_trace(pipeline(), "classify", step_id="routing", version="1")
    examples = (
        SubstitutionExample(
            "a",
            {"text": "urgent"},
            input_ref="data:a",
            quality_ref="labels:a:1",
            expected="support",
        ),
        SubstitutionExample(
            "b",
            {"text": "newsletter"},
            input_ref="data:b",
            quality_ref="labels:b:1",
            expected="marketing",
        ),
    )
    return SubstitutionProtocol(
        "routing substitution",
        "1",
        step,
        examples,
        {"type": "decision", "version": "1.0", "labels": ["support", "marketing"]},
        repetitions=repetitions,
        timeout_s=timeout_s,
    )


def implementation(name, function, *, kind="rule"):
    identity = JudgeIdentity.configured(
        "fixture." + name, "1", {"fixture": name}, provider="local-fixture", model_or_rule=name
    )
    return DecisionImplementation(name, identity, function, kind)


def implementations():
    baseline = implementation(
        "baseline",
        lambda request, **options: DecisionResult(
            "support",
            usage=JudgeUsage(10, 1, 0.02, "reported", "reported"),
            cost_ref="fixture:billing",
        ),
        kind="model",
    )
    rule = implementation(
        "keyword-rule",
        lambda request, **options: DecisionResult(
            "support" if request.inputs["text"] == "urgent" else "marketing", usage=FREE
        ),
    )
    copy_baseline = implementation(
        "baseline-copy", lambda request, **options: DecisionResult("support", usage=FREE)
    )
    return baseline, [rule, copy_baseline]


def bundle():
    baseline, candidates = implementations()
    return run_substitution_experiment(protocol(), baseline, candidates, enabled=True)


def test_runner_preserves_frozen_identity_quality_and_independent_agreement():
    result = bundle()
    assert len(result["records"]) == 6 and len(result["pairs"]) == 4
    assert result["protocol"]["synthetic"] is True
    assert all(item["quality"] is not None for item in result["pairs"])
    candidate = [item for item in result["pairs"] if item["condition"] == "keyword-rule"]
    assert [item["quality"]["candidate_score"] for item in candidate] == [1, 1]
    assert [item["baseline_agreement"] for item in candidate] == [True, False]
    assert all(
        item["baseline_agreement"]
        for item in result["pairs"]
        if item["condition"] == "baseline-copy"
    )


def test_disabled_execution_and_limit_do_not_call_implementations():
    calls = []
    backend = implementation("spy", lambda *args, **kwargs: calls.append(args))
    other = implementation("other", lambda *args, **kwargs: calls.append(args))
    result = run_substitution_experiment(protocol(), backend, [other])
    assert calls == []
    assert all(
        read_trial_evidence(AgentTrace.from_dict(item["trace"]))["outcome"] == "disabled"
        for item in result["records"]
    )
    with pytest.raises(ValueError, match="max_invocations"):
        run_substitution_experiment(protocol(), backend, [other], enabled=True, max_invocations=3)
    assert calls == []


def test_inputs_are_owned_and_reference_answers_are_not_passed_to_candidates():
    seen = []

    def mutate(request, **options):
        assert not hasattr(request, "expected") and not hasattr(request, "quality_ref")
        value = request.inputs
        seen.append(value["text"])
        value["text"] = "changed"
        return DecisionResult("support", usage=FREE)

    plan = protocol()
    original = plan.examples[0].inputs
    run_substitution_experiment(
        plan, implementation("a", mutate), [implementation("b", mutate)], enabled=True
    )
    assert sorted(seen) == ["newsletter", "newsletter", "urgent", "urgent"]
    assert plan.examples[0].inputs == original


def test_trial_receipt_is_owned_and_source_bound():
    result = bundle()
    trace = AgentTrace.from_dict(copy.deepcopy(result["records"][0]["trace"]))
    evidence = read_trial_evidence(trace)
    assert evidence["status"] == "valid"
    evidence["usage"]["cost_usd"] = 100
    assert read_trial_evidence(trace)["usage"]["cost_usd"] != 100
    trace.events[0].duration_ms += 1
    assert read_trial_evidence(trace)["status"] == "invalid"


def test_failed_unknown_and_invalid_outputs_are_retained_with_unknown_usage():
    def fail(request, **options):
        raise RuntimeError("private error")

    baseline, _ = implementations()
    candidates = [
        implementation("fail", fail),
        implementation("unknown", lambda *args, **kwargs: DecisionResult(status="unknown")),
        implementation("invalid", lambda *args, **kwargs: None),
    ]
    result = run_substitution_experiment(protocol(), baseline, candidates, enabled=True)
    assert len(result["records"]) == 8
    assert "private error" not in str(result)
    for row in result["records"]:
        if row["condition"] != "baseline":
            receipt = row["trace"]["metadata"][TRIAL_KEY]
            assert receipt["output_hash"] is None
            assert receipt["usage"]["cost_usd"] is None
    assert all(pair["quality"]["candidate_score"] is None for pair in result["pairs"])


def test_experiments_do_not_attach_to_an_ambient_live_trace():
    seen = []

    def record(request, **options):
        seen.append(current_trace())
        return DecisionResult("support", usage=FREE)

    with trace_agent("outer") as outer:
        run_substitution_experiment(
            protocol(),
            implementation("baseline", record),
            [implementation("candidate", record)],
            enabled=True,
        )
        assert current_trace() is outer and outer.events == []
    assert seen == [None] * 4


def test_quality_is_independent_of_baseline_agreement():
    report = summarize_substitution_experiment(bundle())
    rows = {item["condition"]: item for item in report["comparisons"]}
    assert rows["keyword-rule"]["quality_preserved_on_examples"] is True
    assert rows["keyword-rule"]["observed_candidate_quality"] == 1
    assert rows["keyword-rule"]["baseline_agreement"]["observed_rate"] == 0.5
    assert rows["baseline-copy"]["baseline_agreement"]["observed_rate"] == 1
    assert rows["baseline-copy"]["quality_preserved_on_examples"] is False
    assert report["sample_status"] == "exploratory"


@pytest.mark.parametrize(
    "kind", ["missing", "duplicate", "changed_trial", "changed_scorer", "changed_pair"]
)
def test_incomplete_or_mismatched_artifacts_cannot_pass_quality(kind):
    result = bundle()
    row = next(item for item in result["records"] if item["condition"] == "keyword-rule")
    if kind == "missing":
        result["records"].remove(row)
    elif kind == "duplicate":
        result["records"].append(copy.deepcopy(row))
    elif kind == "changed_trial":
        row["trace"]["events"][0]["duration_ms"] += 1
    else:
        pair = next(item for item in result["pairs"] if item["condition"] == "keyword-rule")
        if kind == "changed_pair":
            pair["candidate_run_id"] = "wrong"
        else:
            pair["quality"]["cases"][0]["scorer"]["version"] = "different"
    report = summarize_substitution_experiment(result)
    comparison = next(item for item in report["comparisons"] if item["condition"] == "keyword-rule")
    assert comparison["quality_preserved_on_examples"] is None
    assert comparison["quality_complete"] is False
    assert comparison["planned_pairs"] == 2


def test_exported_native_studies_preserve_scoped_cost_and_quality_without_reruns(
    tmp_path, monkeypatch
):
    result = bundle()
    original = copy.deepcopy(result)
    monkeypatch.setattr(
        "agentloop.substitutions.build_quality_report",
        lambda *args, **kwargs: pytest.fail("export must not score again"),
    )
    exported = export_substitution_studies(result, tmp_path / "experiment")
    assert len(exported["studies"]) == 2
    first = next(item for item in exported["studies"] if item["name"].endswith(" / keyword-rule"))
    assert first["measurement_scope"] == "declared_decision_step"
    assert first["conditions"]["baseline"]["metrics"]["cost_usd"]["mean"] == 0.02
    assert first["conditions"]["candidate"]["metrics"]["cost_usd"]["mean"] == 0
    assert first["conditions"]["candidate"]["metrics"]["quality_score"]["mean"] == 1
    assert first["comparisons"]["candidate"]["pair_count"] == 2
    assert result == original
    assert export_substitution_studies(result, tmp_path / "experiment") == exported


def test_failed_outputs_stay_in_study_denominators_and_unknown_cost_is_not_zero(tmp_path):
    def fail(*args, **kwargs):
        raise TimeoutError("private")

    baseline, _ = implementations()
    result = run_substitution_experiment(
        protocol(), baseline, [implementation("failing", fail)], enabled=True
    )
    study = export_substitution_studies(result, tmp_path / "failed")["studies"][0]
    condition = study["conditions"]["candidate"]
    assert condition["run_count"] == 2
    assert condition["metrics"]["success"]["mean"] == 0
    assert condition["metrics"]["cost_usd"]["mean"] is None
    assert condition["metrics"]["cost_usd"]["missing_count"] == 2


def test_export_does_not_overwrite_different_artifacts(tmp_path):
    result = bundle()
    root = tmp_path / "experiment"
    export_substitution_studies(result, root)
    (root / "comparison.md").write_text("reviewed user notes", encoding="utf-8")
    with pytest.raises(FileExistsError):
        export_substitution_studies(result, root)
    assert (root / "comparison.md").read_text() == "reviewed user notes"


def score_fixture(output, fixture, scorer):
    return {"passed": True, "score": output}


def test_quality_drop_is_checked_before_float_mean_rounding():
    examples = tuple(
        SubstitutionExample(
            str(index), index, input_ref=f"data:{index}", quality_ref="fixture:scorer-v1"
        )
        for index in range(10)
    )
    plan = SubstitutionProtocol(
        "precision",
        "1",
        protocol().step,
        examples,
        {
            "type": "custom",
            "version": "1",
            "callable": "test_substitutions:score_fixture",
            "pass_score": 0,
        },
        min_quality=0,
        max_quality_drop=0,
    )
    baseline = implementation("baseline", lambda *args, **kwargs: DecisionResult(1.0, usage=FREE))
    candidate = implementation(
        "candidate",
        lambda request, **options: DecisionResult(
            0.9999999999999999 if request.inputs == 0 else 1.0, usage=FREE
        ),
    )
    report = summarize_substitution_experiment(
        run_substitution_experiment(plan, baseline, [candidate], enabled=True)
    )
    assert report["comparisons"][0]["quality_preserved_on_examples"] is False
    assert report["comparisons"][0]["observed_quality_delta"] < 0


def test_frozen_trial_export_does_not_depend_on_live_model_pricing(tmp_path, monkeypatch):
    result = bundle()
    monkeypatch.setenv("AGENTLOOP_PRICING_FILE", str(tmp_path / "missing-pricing.json"))
    exported = export_substitution_studies(result, tmp_path / "frozen")
    assert exported["studies"][0]["conditions"]["baseline"]["metrics"]["cost_usd"]["mean"] == 0.02


def test_edited_derived_report_is_checked_before_any_existing_file_write(tmp_path):
    result = bundle()
    root = tmp_path / "experiment"
    export_substitution_studies(result, root)
    report = root / "candidate-000" / "study-report.md"
    report.write_text("reviewed study notes", encoding="utf-8")
    source = root / "bundle.json"
    before = source.stat().st_mtime_ns
    with pytest.raises(FileExistsError):
        export_substitution_studies(result, root)
    assert source.stat().st_mtime_ns == before


def test_observed_null_is_distinct_from_an_unavailable_output():
    example = SubstitutionExample(
        "null", None, input_ref="data:null", quality_ref="labels:null", expected=None
    )
    plan = SubstitutionProtocol(
        "null",
        "1",
        protocol().step,
        (example,),
        {"type": "decision", "version": "1.0", "labels": [None, "other"]},
    )
    baseline = implementation("baseline", lambda *args, **kwargs: DecisionResult(None, usage=FREE))
    same = implementation("same-null", lambda *args, **kwargs: DecisionResult(None, usage=FREE))
    unknown = implementation("unknown", lambda *args, **kwargs: DecisionResult(status="unknown"))
    result = summarize_substitution_experiment(
        run_substitution_experiment(plan, baseline, [same, unknown], enabled=True)
    )
    comparisons = {item["condition"]: item for item in result["comparisons"]}
    assert comparisons["same-null"]["quality_preserved_on_examples"] is True
    assert comparisons["unknown"]["quality_preserved_on_examples"] is None


def test_late_outputs_are_discarded_without_discarding_known_spend(monkeypatch):
    plan = protocol(timeout_s=1)
    clock = iter(range(0, 32, 2))
    monkeypatch.setattr("agentloop.substitutions.perf_counter", lambda: next(clock))

    def answer(*args, **kwargs):
        return DecisionResult("support", usage=JudgeUsage(cost_usd=0.01, cost_basis="reported"))

    result = run_substitution_experiment(
        plan,
        implementation("baseline", answer),
        [implementation("candidate", answer)],
        enabled=True,
    )
    for row in result["records"]:
        receipt = row["trace"]["metadata"][TRIAL_KEY]
        assert receipt["outcome"] == "timed_out" and receipt["output_hash"] is None
        assert receipt["usage"]["cost_usd"] == 0.01


def test_trial_without_saved_quality_is_not_task_success(tmp_path):
    from agentloop.studies import _run

    result = bundle()
    trace = AgentTrace.from_dict(result["records"][0]["trace"])
    path = trace.export_json(tmp_path / "ungraded.json")
    measured = _run(path, ["plan_hash", "example_id", "repetition"])
    assert measured["metrics"]["quality_score"] is None
    assert measured["metrics"]["success"] is None


def test_removed_trial_receipt_does_not_fall_back_to_zero_cost(tmp_path):
    from agentloop.studies import _run

    trace = AgentTrace.from_dict(bundle()["records"][0]["trace"])
    trace.metadata.pop(TRIAL_KEY)
    row = _run(trace.export_json(tmp_path / "removed.json"), ["example_id"])
    assert row["metrics"]["cost_usd"] is None
    assert row["metrics"]["runtime_ms"] is None
    assert row["metrics"]["success"] is None


def test_native_otel_roundtrip_preserves_frozen_trial_measurements():
    from agentloop import trace_from_otel, trace_to_otel

    trace = AgentTrace.from_dict(bundle()["records"][0]["trace"])
    restored = trace_from_otel(trace_to_otel(trace))
    assert read_trial_evidence(restored) == read_trial_evidence(trace)


def test_mixed_cost_scopes_are_rejected_in_native_studies(tmp_path):
    import json

    from agentloop.studies import StudyValidationError, summarize_study

    export_substitution_studies(bundle(), tmp_path / "scoped")
    root = tmp_path / "scoped" / "candidate-000"
    ordinary = pipeline(run_id="ordinary-control")
    ordinary.export_json(root / "ordinary.json")
    manifest = json.loads((root / "study.json").read_text())
    manifest["conditions"]["candidate"] = ["ordinary.json"]
    (root / "mixed.json").write_text(json.dumps(manifest))
    with pytest.raises(StudyValidationError, match="cannot mix"):
        summarize_study(root / "mixed.json")


def test_protocol_owns_inputs_references_and_scorer_configuration():
    data = {"text": "original"}
    expected = {"label": "support"}
    example = SubstitutionExample(
        "case", data, input_ref="data:case", quality_ref="labels:case", expected=expected
    )
    scorer = {"type": "fields", "version": "1.0", "allow_extra": False}
    plan = SubstitutionProtocol("ownership", "1", protocol().step, [example], scorer)
    before = plan.declaration()
    data["text"] = "changed"
    expected["label"] = "wrong"
    scorer["allow_extra"] = True
    assert plan.declaration() == before
    assert plan.examples[0].inputs == {"text": "original"}


def test_plan_expansion_and_changed_frozen_declarations_are_rejected():
    from agentloop.judgment_types import fingerprint

    result = bundle()
    result["protocol"]["repetitions"] = 1000000
    result["plan_hash"] = fingerprint(
        {key: result[key] for key in ("protocol", "baseline", "implementations")}
    )
    with pytest.raises(ValueError, match="max_invocations"):
        summarize_substitution_experiment(result)


def reject_fixture(output, fixture, scorer):
    return {"passed": False, "score": 1.0}


def test_explicit_scorer_rejection_cannot_pass_from_a_high_score():
    base = protocol()
    plan = SubstitutionProtocol(
        "rejected",
        "1",
        base.step,
        base.examples,
        {
            "type": "custom",
            "version": "1",
            "callable": "test_substitutions:reject_fixture",
            "pass_score": 0,
        },
        min_quality=0,
    )
    baseline, candidates = implementations()
    report = summarize_substitution_experiment(
        run_substitution_experiment(plan, baseline, candidates, enabled=True)
    )
    assert all(item["quality_preserved_on_examples"] is False for item in report["comparisons"])


def test_unexpected_scoring_failure_is_recorded_without_exception_text(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("private scoring data")

    monkeypatch.setattr("agentloop.substitutions.build_quality_report", fail)
    result = bundle()
    assert all(item["quality_error"] == "assessment_failed" for item in result["pairs"])
    assert "private scoring data" not in str(result)
    report = summarize_substitution_experiment(result)
    assert all(
        item["quality_status_counts"] == {"scoring_failed": 2} for item in report["comparisons"]
    )
    assert all(item["quality_preserved_on_examples"] is None for item in report["comparisons"])
