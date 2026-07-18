from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentloop.cli import app
from agentloop.quality import (
    QualityValidationError,
    build_quality_report,
    load_quality_fixtures,
    quality_report_to_markdown,
)


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
        ({"candidate_output": "", "expected": "   "}, "configured and non-empty"),
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


def test_performance_workflow_forwards_quality_fixtures_to_fail_closed_ci() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "agentloop-performance.yml").read_text(
        encoding="utf-8"
    )

    assert 'QUALITY_ARGS+=(--quality-fixtures "$AGENTLOOP_QUALITY_FIXTURES")' in workflow
    assert '"${QUALITY_ARGS[@]}"' in workflow
    assert "--no-fail-on-gate" not in workflow
