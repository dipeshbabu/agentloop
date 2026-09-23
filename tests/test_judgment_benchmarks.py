from __future__ import annotations

import copy
from dataclasses import asdict, replace

import pytest
from workflow_fixtures import pipeline

from agentloop import (
    JudgeIdentity,
    JudgeUsage,
    JudgmentAnswer,
    JudgmentSpec,
    LocalCallbackJudge,
    judgment_request,
)
from agentloop.judgment_adapters import LocalPredictorJudge, TypedServiceJudge
from agentloop.judgment_benchmark_types import JudgmentBenchmark, JudgmentExample
from agentloop.judgment_benchmarks import (
    BenchmarkBackend,
    judgment_benchmark_markdown,
    run_judgment_benchmark,
    summarize_judgment_benchmark,
)
from agentloop.judgment_types import fingerprint

FREE = JudgeUsage(0, 0, 0, "reported", "reported")


def protocol(count=4, repetitions=1, *, labelled=True, kind="boolean"):
    spec = JudgmentSpec("Same approved summaries?", kind)
    examples = []
    for index in range(count):
        trace = pipeline(run_id=f"example-{index}")
        item = judgment_request(
            trace,
            spec,
            ["classify"],
            summaries={"classify": "positive" if index % 2 else "negative"},
        )
        example = (
            JudgmentExample(
                str(index), item, bool(index % 2), "independent", f"fixture:labels:{index}", "1"
            )
            if labelled
            else JudgmentExample(str(index), item)
        )
        examples.append(example)
    return JudgmentBenchmark(
        "synthetic benchmark", "1", tuple(examples), repetitions=repetitions, seed=4, synthetic=True
    )


def identity(name):
    return JudgeIdentity.configured(f"fixture.{name}", "1", {"mode": name})


def backends():
    rule = LocalCallbackJudge(
        identity("rule"),
        lambda item, **options: JudgmentAnswer(item.evidence[0].summary == "positive", usage=FREE),
    )
    local = LocalPredictorJudge(identity("local"), lambda item, **options: False, usage=FREE)
    remote = TypedServiceJudge(
        identity("remote"),
        lambda item, **options: {
            "value": item["evidence"][0]["summary"] == "positive",
            "usage": asdict(FREE),
        },
        credentials_required=False,
    )
    return [
        BenchmarkBackend("rule", rule, "deterministic", True),
        BenchmarkBackend("local", local, "local_predictor", True),
        BenchmarkBackend("remote", remote, "typed_service", True),
    ]


def summary(bundle):
    return {item["name"]: item for item in summarize_judgment_benchmark(bundle)["backends"]}


def test_same_frozen_examples_cover_all_three_adapters_and_keep_quality_separate():
    plan = protocol(repetitions=2)
    bundle = run_judgment_benchmark(plan, backends(), enabled=True)
    report = summarize_judgment_benchmark(bundle)
    models = summary(bundle)
    assert report["planned_invocations"] == report["record_count"] == 24
    assert models["rule"]["quality"][0]["value"] == 1
    assert models["remote"]["quality"][0]["value"] == 1
    assert models["local"]["quality"][0]["value"] == 0.5
    assert all(item["performance"]["cost"]["complete"] for item in models.values())
    assert all(not item["receipt"]["invocation"]["cache_hit"] for item in report["cases"])
    assert "quality" not in report["disagreements"][0]
    assert report["synthetic"] and len(report["simulated_backends"]) == 3
    assert set(report["environment"]) == {"python", "implementation", "system", "architecture"}
    assert JudgmentBenchmark.from_dict(plan.to_dict()) == plan
    assert "Independent-label quality" in judgment_benchmark_markdown(report)


def test_default_disabled_and_call_limit_do_not_execute_backend():
    calls = []
    judge = LocalCallbackJudge(identity("spy"), lambda *args, **kwargs: calls.append(args))
    configurations = [BenchmarkBackend("spy", judge, "deterministic")]
    bundle = run_judgment_benchmark(protocol(), configurations)
    assert summary(bundle)["spy"]["status_counts"] == {"disabled": 4}
    assert calls == []
    with pytest.raises(ValueError, match="max_invocations"):
        run_judgment_benchmark(protocol(), configurations, enabled=True, max_invocations=3)
    assert calls == []


def test_seeded_order_reproducible_and_expected_labels_never_reach_backends():
    seen = []

    def spy(item, **options):
        assert set(item.to_dict()) == {"run_id", "spec", "evidence"}
        seen.append(item.run_id)
        return JudgmentAnswer(False)

    backend = BenchmarkBackend("spy", LocalCallbackJudge(identity("spy"), spy), "deterministic")
    first = run_judgment_benchmark(protocol(), [backend], enabled=True)
    second = run_judgment_benchmark(protocol(), [backend], enabled=True)
    assert seen[:4] == seen[4:]
    assert [item["example_id"] for item in first["records"]] == [
        item["example_id"] for item in second["records"]
    ]
    assert first["plan_hash"] == second["plan_hash"]


def test_agreement_without_independent_labels_is_not_quality():
    bundle = run_judgment_benchmark(protocol(labelled=False), backends(), enabled=True)
    report = summarize_judgment_benchmark(bundle)
    for backend in report["backends"]:
        assert backend["quality"][0]["value"] is None
        assert backend["quality"][0]["observed_value"] is None
        assert backend["quality"][0]["unlabelled_examples"] == 4
    agreement = next(
        item
        for item in report["disagreements"]
        if {item["left_backend"], item["right_backend"]} == {"rule", "remote"}
    )
    assert agreement["observed_mean_difference"] == 0
    assert agreement["observed_pairs"] == 4


@pytest.mark.parametrize(
    "mode",
    [
        "missing",
        "duplicate",
        "invalid_receipt",
        "wrong_request",
        "wrong_judge",
        "reused_invocation",
    ],
)
def test_missing_and_invalid_cases_cannot_improve_full_quality(mode):
    bundle = run_judgment_benchmark(protocol(), backends(), enabled=True)
    row = next(item for item in bundle["records"] if item["backend"] == "rule")
    if mode == "missing":
        bundle["records"].remove(row)
    elif mode == "duplicate":
        bundle["records"].append(copy.deepcopy(row))
    elif mode == "invalid_receipt":
        row["receipt"]["evaluation"]["value"] = "not a boolean"
    elif mode == "wrong_request":
        row["receipt"]["request"]["run_id"] = "wrong-source"
    elif mode == "wrong_judge":
        row["receipt"]["judge"]["version"] = "changed"
    else:
        other = next(
            item for item in bundle["records"] if item["backend"] == "rule" and item is not row
        )
        row["receipt"]["invocation"]["id"] = other["receipt"]["invocation"]["id"]
        row["receipt"]["record_hash"] = fingerprint(
            {key: value for key, value in row["receipt"].items() if key != "record_hash"}
        )
    result = summary(bundle)["rule"]
    assert result["quality"][0]["value"] is None
    assert result["quality"][0]["observed_value"] == 1
    assert result["quality"][0]["coverage"] < 1
    assert result["unavailable_rate"] > 0
    assert not result["performance"]["cost"]["complete"]


def test_missing_credentials_timeout_error_abstention_all_keep_denominators():
    def timeout(*args, **kwargs):
        raise TimeoutError("secret")

    def fail(*args, **kwargs):
        raise RuntimeError("secret")

    configurations = [
        BenchmarkBackend(
            "credentialless", TypedServiceJudge(identity("remote"), fail), "typed_service"
        ),
        BenchmarkBackend(
            "timeout", LocalCallbackJudge(identity("timeout"), timeout), "deterministic"
        ),
        BenchmarkBackend("failure", LocalCallbackJudge(identity("failure"), fail), "deterministic"),
        BenchmarkBackend(
            "abstain",
            LocalCallbackJudge(
                identity("abstain"),
                lambda *args, **kwargs: JudgmentAnswer(
                    status="unknown", reason="insufficient_evidence"
                ),
            ),
            "deterministic",
        ),
    ]
    bundle = run_judgment_benchmark(protocol(), configurations, enabled=True)
    report = summary(bundle)
    assert report["credentialless"]["availability_counts"] == {"missing_credentials": 4}
    assert report["credentialless"]["abstention_rate"] == 1
    assert report["timeout"]["timeout_rate"] == 1
    assert report["failure"]["failure_rate"] == 1
    assert report["abstain"]["abstention_rate"] == 1
    assert all(
        item["planned_invocations"] == 4 and item["unavailable_rate"] == 1
        for item in report.values()
    )
    assert "secret" not in str(bundle)


def test_repeated_incomplete_example_does_not_gain_quality_weight():
    bundle = run_judgment_benchmark(protocol(repetitions=3), backends(), enabled=True)
    row = next(item for item in bundle["records"] if item["backend"] == "rule")
    bundle["records"].remove(row)
    quality = summary(bundle)["rule"]["quality"][0]
    assert quality["complete_labelled_examples"] == 3
    assert quality["incomplete_labelled_examples"] == 1
    assert quality["planned_examples"] == 4
    assert quality["value"] is None and quality["observed_value"] == 1


def test_probability_calibration_requires_supported_labels_and_unique_examples():
    model = BenchmarkBackend(
        "probability",
        LocalPredictorJudge(
            identity("probability"),
            lambda item, **options: 0.8 if item.evidence[0].summary == "positive" else 0.2,
        ),
        "local_predictor",
        True,
    )
    small = summary(
        run_judgment_benchmark(
            protocol(2, repetitions=20, kind="probability"), [model], enabled=True
        )
    )["probability"]["quality"][0]
    assert small["metric"] == "brier_loss" and small["value"] == pytest.approx(0.04)
    assert small["calibration"]["status"] == "insufficient_support"
    assert small["calibration"]["example_count"] == 2
    large = summary(
        run_judgment_benchmark(protocol(20, kind="probability"), [model], enabled=True)
    )["probability"]["quality"][0]
    assert large["calibration"]["status"] == "descriptive"
    assert large["calibration"]["expected_calibration_error"] == pytest.approx(0.2)
    assert large["calibration"]["calibrated"] is False
    assert sum(item["count"] for item in large["calibration"]["bins"]) == 20


def test_single_class_probability_dataset_has_no_calibration_claim():
    plan = protocol(20, kind="probability")
    plan = replace(plan, examples=tuple(replace(item, expected=True) for item in plan.examples))
    model = BenchmarkBackend(
        "probability",
        LocalPredictorJudge(identity("probability"), lambda *args, **kwargs: 0.9),
        "local_predictor",
    )
    result = summary(run_judgment_benchmark(plan, [model], enabled=True))["probability"]["quality"][
        0
    ]
    assert result["calibration"]["status"] == "insufficient_support"


def test_numeric_error_and_disagreement_are_safe_at_extreme_finite_bounds():
    trace = pipeline()
    spec = JudgmentSpec("Bounded score?", "score", minimum=-1e308, maximum=1e308)
    item = JudgmentExample(
        "numeric",
        judgment_request(trace, spec, ["classify"]),
        -1e308,
        "independent",
        "fixture:extreme",
        "1",
    )
    plan = JudgmentBenchmark("extreme", "1", (item,), synthetic=True)
    models = [
        BenchmarkBackend(
            name,
            LocalPredictorJudge(identity(name), lambda *args, value=value, **kwargs: value),
            "local_predictor",
        )
        for name, value in (("left", -1e308), ("right", 1e308))
    ]
    report = summarize_judgment_benchmark(run_judgment_benchmark(plan, models, enabled=True))
    assert report["backends"][1]["quality"][0]["value"] == 1
    assert report["disagreements"][0]["observed_mean_difference"] == 1


def test_summarization_is_readonly_and_unmatched_rows_remain_visible(monkeypatch):
    bundle = run_judgment_benchmark(protocol(), backends(), enabled=True)
    extra = copy.deepcopy(bundle["records"][0])
    extra["example_id"] = "not-planned"
    bundle["records"].append(extra)
    monkeypatch.setattr(
        LocalCallbackJudge, "judge", lambda *args, **kwargs: pytest.fail("must not execute")
    )
    original = copy.deepcopy(bundle)
    report = summarize_judgment_benchmark(bundle)
    assert report["unmatched_records"] == [{"record_index": 12, "reason": "unmatched_record"}]
    assert bundle == original


def test_plan_changes_require_new_fingerprint():
    bundle = run_judgment_benchmark(protocol(), backends(), enabled=True)
    bundle["protocol"]["examples"][0]["expected"] = True
    with pytest.raises(ValueError, match="hash"):
        summarize_judgment_benchmark(bundle)


def test_malformed_invocation_is_an_invalid_slot_not_a_report_crash():
    bundle = run_judgment_benchmark(protocol(), backends(), enabled=True)
    bundle["records"][0]["receipt"]["invocation"] = []
    result = summarize_judgment_benchmark(bundle)
    assert any(item["status"] == "invalid_record" for item in result["cases"])


def test_untrusted_protocol_cannot_expand_unbounded_slots():
    bundle = run_judgment_benchmark(protocol(), backends(), enabled=True)
    bundle["protocol"]["repetitions"] = 1000000
    bundle["plan_hash"] = fingerprint(
        {"protocol": bundle["protocol"], "backends": bundle["backends"]}
    )
    with pytest.raises(ValueError, match="max_invocations"):
        summarize_judgment_benchmark(bundle)


def test_finite_receipts_do_not_overflow_latency_or_cost_aggregation():
    bundle = run_judgment_benchmark(protocol(), backends(), enabled=True)
    for row in bundle["records"]:
        if row["backend"] == "rule":
            receipt = row["receipt"]
            for field in ("evaluation", "invocation"):
                receipt[field]["latency_ms"] = 1e308
                receipt[field]["usage"]["cost_usd"] = 1e308
            receipt["record_hash"] = fingerprint(
                {key: value for key, value in receipt.items() if key != "record_hash"}
            )
    performance = summary(bundle)["rule"]["performance"]
    assert performance["latency_ms"]["mean"] == 1e308
    assert performance["latency_ms"]["median"] == 1e308
    assert performance["cost"]["known_usd"] is None
    assert performance["cost"]["complete"] is False


def test_pairwise_limit_prevents_quadratic_allocation():
    bundle = run_judgment_benchmark(protocol(1), backends(), enabled=True)
    original = bundle["backends"][0]
    bundle["backends"] = [{**original, "name": f"backend-{index}"} for index in range(500)]
    bundle["records"] = []
    bundle["plan_hash"] = fingerprint(
        {"protocol": bundle["protocol"], "backends": bundle["backends"]}
    )
    with pytest.raises(ValueError, match="pairwise"):
        summarize_judgment_benchmark(bundle)
