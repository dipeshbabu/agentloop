from __future__ import annotations

import json

from typer.testing import CliRunner

from agentloop.cli import app
from agentloop.quality import (
    build_quality_report,
    load_quality_fixtures,
    quality_report_to_markdown,
)


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
