from __future__ import annotations

import copy
import json

import pytest
from workflow_fixtures import pipeline

from agentloop import attach_quality_report
from agentloop.quality import (
    QualityValidationError,
    build_quality_report,
    parse_quality_fixtures,
    quality_report_to_markdown,
)
from agentloop.replay import ReplayGates, build_replay_report
from agentloop.structured_quality import EVIDENCE_KEY, read_quality_evidence


def case(kind, expected, actual, *, before=None, identity="case", **settings):
    return {
        "schema_version": "2.0",
        "id": identity,
        "expected": expected,
        "baseline_output": expected if before is None else before,
        "candidate_output": actual,
        "scorer": {"type": kind, **settings},
    }


def exploding_scorer(output, fixture, scorer):
    raise ValueError("private scorer exception")


def timed_out_scorer(output, fixture, scorer):
    raise TimeoutError("private timeout")


def false_scorer(output, fixture, scorer):
    return False


def bad_score(output, fixture, scorer):
    return {"passed": True, "score": 9, "detail": "private detail"}


def near_threshold_scorer(output, fixture, scorer):
    return {"passed": True, "score": 0.99996}


def mutating_scorer(output, fixture, scorer):
    output["changed"] = True
    fixture["expected"] = "changed"
    scorer["configuration"]["key"] = "changed"
    return {"passed": True, "score": 1, "detail": "private scorer detail"}


def test_version_two_is_explicit_and_legacy_results_remain_unchanged():
    payload = {
        "schema_version": "2.0",
        "fixtures": [
            {
                "id": "route",
                "expected": "billing",
                "output": "billing",
                "scorer": {"type": "decision", "labels": ["billing", "other"]},
            }
        ],
    }
    original = copy.deepcopy(payload)
    fixtures = parse_quality_fixtures(payload)
    assert payload == original
    report = build_quality_report(fixtures)
    assert report["schema_version"] == "2.0" and report["passed"]
    assert report["candidate_score"] == 1
    assert report["baseline_score"] is None
    assert report == build_quality_report(fixtures)
    legacy = build_quality_report([{"expected": "billing", "output": "billing"}])
    assert "schema_version" not in legacy
    assert legacy["baseline_score"] == 0
    with pytest.raises(QualityValidationError, match="requires"):
        build_quality_report([{**payload["fixtures"][0]}])
    with pytest.raises(QualityValidationError, match="every structured"):
        build_quality_report([fixtures[0], {"expected": "x", "output": "x"}])


def test_routing_decisions_export_confusion_and_full_denominators():
    fixtures = [
        case("decision", "billing", "billing", identity="correct", labels=["billing", "support"]),
        case("decision", "billing", "support", identity="wrong", labels=["billing", "support"]),
        case(
            "decision",
            "support",
            "unexpected private output",
            identity="unknown",
            labels=["billing", "support"],
        ),
        case("decision", "support", "support", identity="missing", labels=["billing", "support"]),
    ]
    fixtures[-1].pop("candidate_output")
    report = build_quality_report(fixtures)
    assert report["candidate_score"] is None and not report["passed"]
    assert report["summaries"]["candidate"]["assessed_count"] == 3
    assert report["summaries"]["candidate"]["unavailable_count"] == 1
    group = next(iter(report["classification"]["candidate"].values()))
    assert group["matrix_format"] == "sparse_row_column_count"
    assert group["matrix"] == [[0, 0, 1], [0, 1, 1], [1, 2, 1]]
    assert group["paired_output_count"] == 3 and group["case_count"] == 4
    assert group["missing_output_count"] == 1 and group["invalid_prediction_count"] == 1
    assert group["accuracy_on_available_outputs"] == pytest.approx(1 / 3)
    assert "unexpected private output" not in json.dumps(report)


def test_null_is_a_known_label_and_booleans_do_not_alias_numbers():
    report = build_quality_report(
        [
            case("decision", None, None, labels=[None, False, 0]),
            case("decision", False, 0, identity="typed", labels=[None, False, 0]),
        ]
    )
    assert report["cases"][0]["candidate"]["score"] == 1
    assert report["cases"][1]["candidate"]["score"] == 0
    assert report["cases"][0]["candidate"]["output_present"]


@pytest.mark.parametrize(
    "expected,actual,score,state",
    [
        ([], [], 1, "evaluated"),
        (["a", "b"], ["a"], 2 / 3, "evaluated"),
        (["a"], ["a", "unknown"], 2 / 3, "unknown_label"),
        (["a"], ["a", "a"], 0, "invalid_output"),
    ],
)
def test_multilabel_sets_are_scored_without_duplicate_inflation(expected, actual, score, state):
    result = build_quality_report([case("multilabel", expected, actual, labels=["a", "b"])])[
        "cases"
    ][0]["candidate"]
    assert result["score"] == pytest.approx(score) and result["status"] == state


def test_numeric_bounds_and_declared_decimal_tolerance():
    fixture = case("numeric", 0.9, 0.85, minimum=0, maximum=1, tolerance=0.05)
    assert build_quality_report([fixture])["passed"]
    fixture["candidate_output"] = True
    result = build_quality_report([fixture])["cases"][0]["candidate"]
    assert result["status"] == "invalid_output" and result["score"] == 0
    fixture["candidate_output"] = 1.1
    assert not build_quality_report([fixture])["passed"]


def test_extraction_fields_preserve_structured_types_and_thresholds():
    fixture = case(
        "fields",
        {"name": "Ada", "count": 1, "tags": ["x"]},
        {"name": "Ada", "count": True, "tags": ["x"], "extra": 2},
        pass_score=0.6,
    )
    report = build_quality_report([fixture])
    assert report["candidate_score"] == pytest.approx(2 / 3)
    assert report["passed"]
    fixture["scorer"]["allow_extra"] = False
    assert build_quality_report([fixture])["candidate_score"] == 0.5
    assert not build_quality_report([fixture])["passed"]


@pytest.mark.parametrize("symmetric,expected", [(False, 0), (True, 1)])
def test_entity_matches_support_declared_direction(symmetric, expected):
    fixture = case("matches", [["a", 1]], [[1, "a"]], symmetric=symmetric)
    result = build_quality_report([fixture])["cases"][0]["candidate"]
    assert result["score"] == expected


def test_invalid_references_remain_unscored_instead_of_blame_on_output():
    fixtures = [
        case("decision", {"bad": "reference"}, "x", identity="decision", labels=["x"]),
        case("matches", [[{"bad": 1}, "a"]], [["b", "a"]], identity="pairs"),
        case("multilabel", [{"bad": 1}], ["x"], identity="labels", labels=["x"]),
    ]
    report = build_quality_report(fixtures)
    assert report["candidate_score"] is None
    assert all(item["candidate"]["status"] == "invalid_expected" for item in report["cases"])


@pytest.mark.parametrize(
    "function,status",
    [
        (exploding_scorer, "scorer_error"),
        (timed_out_scorer, "scorer_timeout"),
        (bad_score, "scorer_error"),
    ],
)
def test_custom_failures_are_explicit_without_exception_bodies(function, status):
    fixture = case(
        "custom", True, True, callable=f"{__name__}:{function.__name__}", version="test-v1"
    )
    report = build_quality_report([fixture])
    assert report["candidate_score"] is None
    assert report["cases"][0]["candidate"]["status"] == status
    assert "private" not in json.dumps(report)


def test_custom_false_is_not_overridden_by_a_numeric_threshold():
    fixture = case(
        "custom", True, True, callable=f"{__name__}:false_scorer", version="test-v1", pass_score=0
    )
    assert not build_quality_report([fixture])["passed"]


def test_aggregate_threshold_is_checked_before_display_rounding():
    fixture = case(
        "custom",
        True,
        True,
        callable=f"{__name__}:near_threshold_scorer",
        version="1",
        pass_score=0.99,
    )
    report = build_quality_report([fixture], min_score=1)
    assert not report["passed"]
    assert report["candidate_score"] < 1
    assert "0.99996" in quality_report_to_markdown(report)


def test_custom_input_mutations_are_isolated_and_configuration_is_hashed():
    fixture = case(
        "custom",
        {"x": 1},
        {"x": 1},
        callable=f"{__name__}:mutating_scorer",
        version="test-v1",
        configuration={"key": "private configuration"},
    )
    original = copy.deepcopy(fixture)
    report = build_quality_report([fixture])
    assert fixture == original and report["passed"]
    assert "private configuration" not in json.dumps(report)
    assert "private scorer detail" not in json.dumps(report)
    assert len(report["cases"][0]["scorer"]["configuration_hash"]) == 64


def test_workflow_does_not_guess_its_final_output_from_an_intermediate_model():
    from agentloop.tracer import record_model_call

    trace = pipeline()
    record_model_call(
        "intermediate",
        duration_ms=1,
        started_at=trace.started_at,
        ended_at=trace.ended_at,
        output_text="billing",
        trace=trace,
    )
    fixture = case("decision", "billing", "billing", labels=["billing"])
    fixture.pop("candidate_output")
    result = build_quality_report([fixture], candidate_trace=trace)
    assert result["cases"][0]["candidate"]["status"] == "missing_output"


def test_replay_preserves_unknown_quality_and_fails_closed():
    before, after = pipeline(), pipeline(run_id="candidate")
    fixture = case("decision", "billing", "billing", labels=["billing"])
    fixture.pop("candidate_output")
    quality = build_quality_report([fixture], baseline_trace=before, candidate_trace=after)
    replay = build_replay_report(
        before, after, quality_report=quality, gates=ReplayGates(min_quality_score=0.9)
    )
    assert replay["candidate"]["quality_score"] is None
    assert replay["deltas"]["quality_score_delta"] is None
    assert not replay["gates"]["passed"]
    gate = next(item for item in replay["gates"]["results"] if item["name"] == "quality_fixtures")
    assert gate["indeterminate"]
    assert replay["candidate"]["decision_count"] == 2


def test_execution_failure_cannot_pass_from_correct_partial_output():
    trace = pipeline(status="failed")
    quality = build_quality_report(
        [case("decision", "x", "x", labels=["x"])], candidate_trace=trace
    )
    assert quality["cases"][0]["candidate"]["score"] == 1
    assert quality["candidate_score"] is None and not quality["passed"]


def test_attachment_preserves_aggregate_threshold_and_does_not_clobber_host_metadata():
    trace = pipeline()
    trace.metadata["quality_score"] = 1.0
    quality = build_quality_report(
        [case("fields", {"a": 1, "b": 2}, {"a": 1, "b": 0}, pass_score=0.5)],
        candidate_trace=trace,
        min_score=0.8,
    )
    assert not quality["passed"] and quality["failed_case_count"] == 0
    attach_quality_report(trace, quality)
    assert trace.metadata["quality_score"] == 1.0
    evidence = read_quality_evidence(trace)
    assert evidence["score"] == 0.5 and not evidence["passed"]
    assert trace.report()["quality_score"] == 0.5


def test_invalid_attached_quality_does_not_fall_back_to_an_old_score():
    trace = pipeline()
    trace.metadata["quality_score"] = 1.0
    quality = build_quality_report(
        [case("decision", "x", "x", labels=["x"])], candidate_trace=trace
    )
    attach_quality_report(trace, quality)
    trace.metadata[EVIDENCE_KEY]["summary"]["score"] = 0.9
    assert trace.report()["quality_evidence"]["status"] == "invalid"
    assert trace.report()["quality_score"] is None


def test_large_finite_numeric_difference_does_not_overflow_quality_scoring():
    result = build_quality_report([case("numeric", 1e308, -1e308, minimum=-1e308, maximum=1e308)])[
        "cases"
    ][0]["candidate"]
    assert result["score"] == 0
    assert result["counts"]["absolute_error"] is None
    assert result["counts"]["absolute_error_status"] == "numeric_range"


def test_builtin_parser_failure_is_explicit_and_cannot_pass(monkeypatch):
    from agentloop import quality

    original = quality._json_value

    def parse(value):
        if value == "parser-limit":
            raise RecursionError("private parser context")
        return original(value)

    monkeypatch.setattr(quality, "_json_value", parse)
    report = build_quality_report([case("fields", {"value": 1}, "parser-limit")])
    assert report["candidate_score"] is None and not report["passed"]
    assert report["cases"][0]["candidate"]["status"] == "scorer_error"


def test_known_task_failure_cannot_be_overridden_by_fixture_status():
    trace = pipeline()
    trace.metadata["success"] = False
    fixture = case("decision", "x", "x", labels=["x"])
    fixture["candidate_status"] = "completed"
    report = build_quality_report([fixture], candidate_trace=trace)
    assert not report["passed"]
    assert report["cases"][0]["candidate"]["execution_status"] == "failed"


def test_confusion_reporting_preserves_parser_failures(monkeypatch):
    from agentloop import quality

    original = quality._json_value

    def parse(value):
        if value == "parser-limit":
            raise RecursionError("private context")
        return original(value)

    monkeypatch.setattr(quality, "_json_value", parse)
    report = build_quality_report(
        [
            case(
                "decision",
                "x",
                "parser-limit",
                before={"category": "x"},
                labels=["x"],
                field="category",
            )
        ]
    )
    group = next(iter(report["classification"]["candidate"].values()))
    assert report["candidate_score"] is None
    assert group["unassessed_output_count"] == 1 and group["paired_output_count"] == 0


def test_markdown_renders_unknown_scores_and_statuses():
    fixture = case("decision", "x", "x", labels=["x"])
    fixture.pop("candidate_output")
    markdown = quality_report_to_markdown(build_quality_report([fixture]))
    assert (
        "unavailable" in markdown and "missing_output" in markdown and "Assessed: 0/1" in markdown
    )
