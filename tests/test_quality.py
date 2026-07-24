from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentloop.cli import app
from agentloop.events import AgentEvent, utc_now_iso
from agentloop.quality import (
    QualityValidationError,
    build_quality_report,
    load_quality_fixtures,
    parse_quality_fixtures,
    quality_report_to_markdown,
)
from agentloop.tracer import AgentTrace


def out_of_range_scorer(output, fixture, scorer):  # type: ignore[no-untyped-def]
    return {"passed": True, "score": 1.01}


def test_quality_report_scores_required_fields_and_glob(tmp_path) -> None:
    fixtures = [
        {
            "id": "fields",
            "candidate_output": {"summary": "done", "sources": ["a"]},
            "baseline_output": {"summary": "done", "sources": ["a"]},
            "scorer": {"type": "required_fields", "required": ["summary", "sources"]},
        },
        {
            "id": "glob",
            "candidate_output": "answer: 42",
            "baseline_output": "answer: 42",
            "scorer": {"type": "glob", "pattern": "answer: [0-9][0-9]"},
        },
    ]

    report = build_quality_report(fixtures, min_score=1.0)
    markdown = quality_report_to_markdown(report)

    assert report["passed"] is True
    assert report["candidate_score"] == 1.0
    assert "AgentLoop Quality Report" in markdown


def test_quality_report_rejects_raw_regex_scorers() -> None:
    report = build_quality_report(
        [
            {
                "id": "unsafe-regex",
                "candidate_output": "a" * 10_000 + "!",
                "baseline_output": "unused",
                "scorer": {"type": "regex", "pattern": "(a+)+$"},
            }
        ]
    )

    assert report["passed"] is False
    assert "disabled" in report["cases"][0]["candidate"]["detail"]


def test_quality_report_bounds_glob_inputs() -> None:
    report = build_quality_report(
        [
            {
                "id": "oversized-glob",
                "candidate_output": "ok",
                "scorer": {"type": "glob", "pattern": "*" * 257},
            }
        ]
    )

    assert report["passed"] is False
    assert "safety limit" in report["cases"][0]["candidate"]["detail"]


def test_load_quality_fixtures_accepts_list_or_wrapped_object(tmp_path) -> None:
    path = tmp_path / "fixtures.json"
    path.write_text(json.dumps({"fixtures": [{"id": "case", "expected": "ok"}]}), encoding="utf-8")

    assert load_quality_fixtures(path)[0]["id"] == "case"


@pytest.mark.parametrize(
    ("actual", "expected"),
    [
        pytest.param(False, 0, id="boolean-vs-integer"),
        pytest.param(0, False, id="integer-vs-boolean"),
        pytest.param(None, "", id="null-vs-empty-string"),
        pytest.param("", None, id="empty-string-vs-null"),
        pytest.param(0, "0", id="integer-vs-string"),
        pytest.param(1, 1.0, id="integer-vs-float"),
        pytest.param({"value": False}, {"value": 0}, id="nested-object-types"),
        pytest.param([None], [""], id="nested-array-types"),
        pytest.param(" answer ", "answer", id="string-whitespace"),
    ],
)
def test_exact_match_does_not_coerce_types_or_whitespace(actual, expected) -> None:
    report = build_quality_report(
        [{"candidate_output": actual, "expected": expected, "scorer": {"type": "exact_match"}}]
    )

    assert report["passed"] is False
    assert report["candidate_score"] == 0.0
    assert report["failed_case_count"] == 1


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(False, id="false"),
        pytest.param(0, id="zero"),
        pytest.param(None, id="null"),
        pytest.param("", id="empty-string"),
        pytest.param([], id="empty-array"),
        pytest.param({}, id="empty-object"),
    ],
)
def test_exact_match_accepts_explicit_falsey_expectations(value) -> None:
    report = build_quality_report(
        [{"candidate_output": value, "expected": value, "scorer": {"type": "exact_match"}}]
    )

    assert report["passed"] is True
    assert report["candidate_score"] == 1.0


@pytest.mark.parametrize(
    "expected",
    [
        pytest.param(False, id="false"),
        pytest.param(0, id="zero"),
        pytest.param(None, id="null"),
        pytest.param("", id="empty-string"),
        pytest.param([], id="empty-array"),
        pytest.param({}, id="empty-object"),
    ],
)
def test_exact_match_never_treats_missing_output_as_falsey_value(expected) -> None:
    report = build_quality_report([{"expected": expected, "scorer": {"type": "exact_match"}}])

    assert report["passed"] is False
    assert report["cases"][0]["candidate"]["detail"] == "output is missing"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(None, id="null"),
        pytest.param("", id="empty-string"),
    ],
)
def test_exact_match_preserves_explicit_empty_trace_output(value) -> None:
    candidate = AgentTrace(name="candidate")
    candidate.metadata["output"] = value

    report = build_quality_report(
        [{"expected": value, "scorer": {"type": "exact_match"}}],
        candidate_trace=candidate,
    )

    assert report["passed"] is True


def test_exact_match_preserves_empty_model_event_output() -> None:
    candidate = AgentTrace(name="candidate")
    now = utc_now_iso()
    candidate.add_event(
        AgentEvent(
            event_id="evt_empty_output",
            run_id=candidate.run_id,
            event_type="model_call",
            name="answer",
            started_at=now,
            ended_at=now,
            duration_ms=0,
            output_text="",
        )
    )

    report = build_quality_report(
        [{"expected": "", "scorer": {"type": "exact_match"}}],
        candidate_trace=candidate,
    )

    assert report["passed"] is True


@pytest.mark.parametrize(
    ("output", "expected", "passed", "detail"),
    [
        pytest.param({}, {"result": None}, False, "missing keys", id="missing-null"),
        pytest.param(
            {"result": None},
            {"result": None},
            True,
            "JSON subset matched",
            id="present-null",
        ),
        pytest.param(
            {"result": False},
            {"result": 0},
            False,
            "mismatched keys",
            id="boolean-vs-integer",
        ),
        pytest.param(
            {"result": {"ok": False}},
            {"result": {"ok": 0}},
            False,
            "mismatched keys",
            id="nested-types",
        ),
    ],
)
def test_json_subset_requires_present_type_exact_values(output, expected, passed, detail) -> None:
    report = build_quality_report(
        [
            {
                "candidate_output": output,
                "scorer": {"type": "json_subset", "expected": expected},
            }
        ]
    )

    assert report["passed"] is passed
    assert detail in report["cases"][0]["candidate"]["detail"]


def test_dashboard_fixture_parsing_preserves_falsey_json_values() -> None:
    fixtures = parse_quality_fixtures(
        json.loads(
            """
            {
              "fixtures": [
                {"candidate_output": false, "expected": 0},
                {
                  "candidate_output": {},
                  "scorer": {"type": "json_subset", "expected": {"result": null}}
                }
              ]
            }
            """
        )
    )

    report = build_quality_report(fixtures, min_score=1.0)

    assert report["passed"] is False
    assert report["candidate_score"] == 0.0
    assert report["failed_case_count"] == 2


def test_text_scorers_preserve_falsey_scalar_output() -> None:
    report = build_quality_report(
        [
            {
                "candidate_output": 0,
                "scorer": {"type": "contains", "text": "0"},
            },
            {
                "candidate_output": False,
                "scorer": {"type": "glob", "pattern": "F*"},
            },
        ]
    )

    assert report["passed"] is True


@pytest.mark.parametrize("payload", [[], {"fixtures": []}])
def test_load_quality_fixtures_rejects_empty_suites(tmp_path, payload) -> None:
    path = tmp_path / "fixtures.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(QualityValidationError, match="at least one case"):
        load_quality_fixtures(path)


@pytest.mark.parametrize(
    "fixture, message",
    [
        ({"candidate_output": "ok", "scorer": {"type": "contains"}}, "non-empty"),
        ({"candidate_output": ""}, "requires an explicit 'expected' value"),
        (
            {"candidate_output": {}, "scorer": {"type": "required_fields"}},
            "non-empty field list",
        ),
        (
            {"candidate_output": {}, "scorer": {"type": "json_subset", "expected": {}}},
            "non-empty expected object",
        ),
    ],
)
def test_quality_report_rejects_vacuous_scorers(fixture, message) -> None:
    with pytest.raises(QualityValidationError, match=message):
        build_quality_report([fixture])


def test_quality_report_rejects_noncanonical_scorer_type() -> None:
    with pytest.raises(QualityValidationError, match="leading or trailing whitespace"):
        build_quality_report(
            [
                {
                    "candidate_output": "ok",
                    "expected": "ok",
                    "scorer": {"type": " exact_match "},
                }
            ]
        )


@pytest.mark.parametrize("min_score", [-0.01, 1.01, float("nan")])
def test_quality_report_rejects_out_of_range_min_score(min_score) -> None:
    with pytest.raises(QualityValidationError, match="between 0 and 1"):
        build_quality_report([{"candidate_output": "ok", "expected": "ok"}], min_score=min_score)


def test_quality_report_rejects_out_of_range_custom_scores() -> None:
    fixture = {
        "candidate_output": "ok",
        "expected": "ok",
        "scorer": {"type": "custom", "callable": f"{__name__}:out_of_range_scorer"},
    }

    with pytest.raises(QualityValidationError, match="score must be between 0 and 1"):
        build_quality_report([fixture])


def test_cli_quality_report_writes_outputs(tmp_path) -> None:
    fixtures = tmp_path / "fixtures.json"
    out = tmp_path / "quality.md"
    json_out = tmp_path / "quality.json"
    fixtures.write_text(
        json.dumps(
            {
                "fixtures": [
                    {
                        "id": "exact",
                        "candidate_output": "ok",
                        "baseline_output": "ok",
                        "expected": "ok",
                        "scorer": {"type": "exact_match"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "quality-report",
            str(fixtures),
            "--out",
            str(out),
            "--json-out",
            str(json_out),
            "--min-score",
            "1.0",
        ],
    )

    assert result.exit_code == 0
    assert out.exists()
    assert json.loads(json_out.read_text(encoding="utf-8"))["passed"] is True


def test_cli_quality_report_rejects_empty_suite_without_writing_outputs(tmp_path) -> None:
    fixtures = tmp_path / "fixtures.json"
    out = tmp_path / "quality.md"
    json_out = tmp_path / "quality.json"
    fixtures.write_text('{"fixtures": []}', encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "quality-report",
            str(fixtures),
            "--out",
            str(out),
            "--json-out",
            str(json_out),
        ],
    )

    assert result.exit_code == 2
    assert "must contain at" in result.output
    assert "least one case" in result.output
    assert not out.exists()
    assert not json_out.exists()


def test_cli_quality_report_fails_falsey_type_and_missing_key_cases(tmp_path) -> None:
    fixtures = tmp_path / "fixtures.json"
    out = tmp_path / "quality.md"
    json_out = tmp_path / "quality.json"
    fixtures.write_text(
        json.dumps(
            {
                "fixtures": [
                    {"candidate_output": False, "expected": 0},
                    {
                        "candidate_output": {},
                        "scorer": {
                            "type": "json_subset",
                            "expected": {"result": None},
                        },
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        [
            "quality-report",
            str(fixtures),
            "--out",
            str(out),
            "--json-out",
            str(json_out),
            "--min-score",
            "1.0",
        ],
    )

    assert result.exit_code == 1
    assert out.exists()
    report = json.loads(json_out.read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert report["candidate_score"] == 0.0
    assert report["failed_case_count"] == 2


def test_performance_workflow_forwards_quality_fixtures_to_fail_closed_ci() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "agentloop-performance.yml").read_text(
        encoding="utf-8"
    )

    assert 'QUALITY_ARGS+=(--quality-fixtures "$AGENTLOOP_QUALITY_FIXTURES")' in workflow
    assert '"${QUALITY_ARGS[@]}"' in workflow
    assert "--no-fail-on-gate" not in workflow
