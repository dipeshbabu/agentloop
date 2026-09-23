from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from agentloop.finding_benchmark_types import FindingBenchmark
from agentloop.finding_benchmarks import (
    compare_finding_benchmarks,
    finding_benchmark_markdown,
    run_finding_benchmark,
    summarize_finding_benchmark,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def corpus():
    return FindingBenchmark(
        json.loads((ROOT / "examples/finding_benchmark_corpus.json").read_text(encoding="utf-8"))
    )


@pytest.fixture(scope="module")
def evaluated(corpus):
    # Metric tests use archived output so improvements do not have to preserve
    # known detector defects. The separate release test evaluates current code.
    return json.loads(
        (ROOT / "examples/finding_benchmark_baseline.json").read_text(encoding="utf-8")
    )


def row(result, identity):
    return next(item for item in result["rows"] if item["case_id"] == identity)


def test_independent_labels_expose_existing_false_positives(corpus, evaluated):
    summary = summarize_finding_benchmark(corpus, evaluated)
    assert summary["overall"]["planned_cases"] == 28
    assert summary["overall"]["true_positive_findings"] == 8
    assert summary["overall"]["false_positive_findings"] == 2
    assert summary["overall"]["unknown_cases"] == 6
    assert summary["overall"]["ambiguous_cases"] == 5
    assert summary["overall"]["unscored_findings"] == 1
    per_rule = {item["rule_id"]: item for item in summary["per_rule"]}
    assert per_rule["batch_model_calls"]["false_positive_rate"] == 1
    assert per_rule["add_schema_validation"]["precision"] == 0.5
    assert summary["overall"]["confidence_counts"] == []
    semantic = per_rule["semantic_redundancy"]
    assert semantic["rule_version"] == "1.0"
    assert semantic["backends"][0]["config_hash"]
    assert semantic["confidence_counts"][0]["basis"] == "semantic_judgment"
    assert semantic["abstention_rate"] == 3 / 4


def test_frozen_plan_owned_and_roundtrippable(corpus):
    value = corpus.to_dict()
    clone = FindingBenchmark(value)
    value["cases"][0]["trace"]["events"].clear()
    returned = clone.to_dict()
    returned["cases"].clear()
    assert clone.corpus_hash == corpus.corpus_hash
    assert clone.to_dict() == corpus.to_dict()


@pytest.mark.parametrize(
    "mutation",
    [
        "schema",
        "duplicate",
        "group_leak",
        "trace_leak",
        "invalid_span",
        "unlabelled_targets",
        "missing_provenance",
        "duplicate_target",
        "evidence",
        "invalid_trace",
    ],
)
def test_invalid_or_leaking_corpus_rejected(corpus, mutation):
    value = corpus.to_dict()
    case = value["cases"][0]
    if mutation == "schema":
        value["schema_version"] = "900"
    elif mutation == "duplicate":
        value["cases"].append(copy.deepcopy(case))
    elif mutation == "group_leak":
        value["cases"][2]["group"] = case["group"]
    elif mutation == "trace_leak":
        value["cases"][2]["trace"] = case["trace"]
        value["cases"][2]["opportunities"] = case["opportunities"]
    elif mutation == "invalid_span":
        case["opportunities"][0]["spans"] = ["absent"]
    elif mutation == "unlabelled_targets":
        case["label"] = "unknown"
    elif mutation == "missing_provenance":
        case["label_ref"] = ""
    elif mutation == "duplicate_target":
        case["opportunities"].append(copy.deepcopy(case["opportunities"][0]))
    elif mutation == "evidence":
        case["required_evidence"] = [{}]
    else:
        case["trace"]["events"][0]["duration_ms"] = -1
    with pytest.raises(ValueError):
        FindingBenchmark(value)


def test_run_is_label_blind_and_never_calls_a_judge(corpus, monkeypatch):
    from agentloop import AgentTrace
    from agentloop.judgments import LocalCallbackJudge

    calls, original = [], AgentTrace.report

    def inspect(trace):
        calls.append(trace.run_id)
        assert "label" not in trace.metadata and "opportunities" not in trace.metadata
        return original(trace)

    monkeypatch.setattr(AgentTrace, "report", inspect)
    monkeypatch.setattr(
        LocalCallbackJudge,
        "judge",
        lambda *args, **kwargs: pytest.fail("saved analysis invoked a judge"),
    )
    result = run_finding_benchmark(corpus, split="evaluation", source_revision="test")
    assert len(calls) == len(set(calls)) == len(result["rows"]) == 28


def test_rule_failure_and_analysis_failure_are_retained(corpus, monkeypatch):
    from agentloop import AgentTrace, rules

    def broken(context):
        raise ValueError("private fixture text")

    monkeypatch.setattr(
        rules,
        "BUILTIN_RULES",
        tuple(
            rules.FindingRule(item.rule_id, item.version, broken)
            if item.rule_id == "parallelize_tools"
            else item
            for item in rules.BUILTIN_RULES
        ),
    )
    result = run_finding_benchmark(corpus, split="evaluation", source_revision="test")
    assert result["summary"]["overall"]["error_cases"] == 4
    assert "private fixture text" not in json.dumps(result)
    monkeypatch.setattr(AgentTrace, "report", broken)
    result = run_finding_benchmark(corpus, split="evaluation", source_revision="test")
    assert result["summary"]["overall"]["error_cases"] == 28


def test_missing_cases_stay_in_denominator_and_cannot_be_reviewed_away(corpus, evaluated):
    candidate = copy.deepcopy(evaluated)
    candidate["rows"] = candidate["rows"][1:]
    candidate["summary"] = {"precision": 1}  # Never trusted.
    summary = summarize_finding_benchmark(corpus, candidate)["overall"]
    assert summary["planned_cases"] == 28 and summary["missing_cases"] == 1
    gate = compare_finding_benchmarks(corpus, evaluated, candidate, review_ref="review:123")
    assert not gate["passed"] and not gate["review_applied"]


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "unknown_case",
        "corpus_hash",
        "trace_hash",
        "version",
        "status",
        "nan",
        "policy",
        "rule_removed",
    ],
)
def test_tampered_results_fail_closed(corpus, evaluated, mutation):
    result = copy.deepcopy(evaluated)
    first = result["rows"][0]
    if mutation == "duplicate":
        result["rows"].append(first)
    elif mutation == "unknown_case":
        first["case_id"] = "absent"
    elif mutation == "corpus_hash":
        result["corpus_hash"] = "changed"
    elif mutation == "trace_hash":
        first["trace_hash"] = "changed"
    elif mutation == "version":
        first["rule_version"] = "changed"
    elif mutation == "status":
        first["status"] = "abstained"
    elif mutation == "nan":
        first["analysis_ms"] = float("nan")
    elif mutation == "policy":
        result["policy"]["ref"] = "tuned-after-evaluation"
    else:
        del result["rules"][first["rule_id"]]
    with pytest.raises(ValueError):
        summarize_finding_benchmark(corpus, result)


def test_exact_spans_duplicate_findings_and_precision_gate(corpus, evaluated):
    candidate = copy.deepcopy(evaluated)
    first = row(candidate, "parallel-independent-evaluation")
    first["findings"].append(copy.deepcopy(first["findings"][0]))
    gate = compare_finding_benchmarks(corpus, evaluated, candidate)
    assert not gate["passed"]
    assert any(item["metric"] == "false_positive_findings" for item in gate["regressions"])
    assert compare_finding_benchmarks(
        corpus, evaluated, candidate, review_ref="review:duplicate-regression"
    )["review_applied"]
    first["findings"] = first["findings"][:1]
    first["findings"][0]["affected_nodes"] = ["span-0"]
    gate = compare_finding_benchmarks(corpus, evaluated, candidate)
    assert {item["metric"] for item in gate["regressions"]} == {
        "false_positive_findings",
        "missed_opportunities",
    }


def test_abstain_everywhere_cannot_improve_gate(corpus, evaluated):
    candidate = copy.deepcopy(evaluated)
    for item in candidate["rows"]:
        item.update(status="abstained", findings=[])
    gate = compare_finding_benchmarks(corpus, evaluated, candidate)
    assert not gate["passed"] and len(gate["regressions"]) == 8
    summary = summarize_finding_benchmark(corpus, candidate)["overall"]
    assert summary["precision"] is None and summary["abstention_rate"] == 1


def test_unknown_is_not_negative_and_unscored_emissions_are_gated(corpus, evaluated):
    candidate = copy.deepcopy(evaluated)
    target = row(candidate, "semantic_redundancy-unknown")
    target["findings"] = copy.deepcopy(row(candidate, "semantic_redundancy-supported")["findings"])
    target["status"] = "emitted"
    summary = summarize_finding_benchmark(corpus, candidate)["overall"]
    assert summary["false_positive_findings"] == 2 and summary["unscored_findings"] == 2
    gate = compare_finding_benchmarks(corpus, evaluated, candidate)
    assert not gate["passed"]
    assert {item["metric"] for item in gate["regressions"]} == {
        "unscored_findings",
        "incomplete_evidence_findings",
    }


def test_review_cannot_override_development_split_or_changed_corpus(corpus, evaluated):
    development = run_finding_benchmark(corpus, split="development", source_revision="test")
    with pytest.raises(ValueError, match="held-out"):
        compare_finding_benchmarks(corpus, evaluated, development, review_ref="review:123")
    assert compare_finding_benchmarks(corpus, evaluated, evaluated)["passed"]
    markdown = finding_benchmark_markdown(corpus, evaluated)
    assert "not universal precision" in markdown and "| batch_model_calls | 1.1 |" in markdown


def test_baseline_release_gate(corpus, evaluated):
    baseline = json.loads(
        (ROOT / "examples/finding_benchmark_baseline.json").read_text(encoding="utf-8")
    )
    current = run_finding_benchmark(corpus, split="evaluation", source_revision="test:current")
    assert compare_finding_benchmarks(corpus, baseline, current)["passed"]


def test_cli_preserves_failure_evidence_and_refuses_clobber(
    corpus, evaluated, tmp_path, monkeypatch
):
    import sys

    from scripts import benchmark_findings
    from scripts.benchmark_findings import main

    monkeypatch.setattr(
        benchmark_findings,
        "run_finding_benchmark",
        lambda *args, **kwargs: copy.deepcopy(evaluated),
    )

    baseline = copy.deepcopy(evaluated)
    for identity in ("network-retry", "batch-stateful-shift"):
        row(baseline, identity).update(status="abstained", findings=[])
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    out = tmp_path / "result"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark_findings",
            str(ROOT / "examples/finding_benchmark_corpus.json"),
            "--split",
            "evaluation",
            "--source-revision",
            "test:cli",
            "--baseline",
            str(baseline_path),
            "--out",
            str(out),
        ],
    )
    assert main() == 1
    gate = json.loads((out / "gate.json").read_text(encoding="utf-8"))
    assert not gate["passed"] and len(gate["regressions"]) == 4
    assert len(json.loads((out / "results.json").read_text(encoding="utf-8"))["rows"]) == 28
    assert "Release gate: FAIL" in (out / "report.md").read_text(encoding="utf-8")
    with pytest.raises(SystemExit):
        main()
