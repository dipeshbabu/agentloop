from __future__ import annotations

from agentloop.ci import ci_report_to_markdown
from agentloop.exporters import export_report_markdown
from agentloop.findings import diagnosis_to_markdown
from agentloop.issues import build_issue_drafts, issue_drafts_to_markdown
from agentloop.patches import patch_plan_to_markdown
from agentloop.plan_export import export_optimization_markdown
from agentloop.quality import quality_report_to_markdown
from agentloop.replay import replay_report_to_markdown

HOSTILE_TEXT = "alpha|beta\r\n## injected\n<script>[link](x) *bold* _em_ \\ 雪"
HOSTILE_CODE = "code `one` ``two`` ```three```\r\n<script>"


def test_report_export_escapes_trace_text_by_markdown_context(tmp_path) -> None:
    report = {
        "name": HOSTILE_TEXT,
        "run_id": HOSTILE_CODE,
        "total_runtime_ms": 1000.0,
        "estimated_cost_usd": 0.1,
        "model_call_count": 1,
        "tool_call_count": 1,
        "retry_count": 0,
        "input_tokens": 10,
        "output_tokens": 5,
        "repeated_context_ratio": 0.0,
        "recommendations": [{"title": HOSTILE_TEXT, "description": HOSTILE_TEXT}],
        "events": [
            {
                "event_type": HOSTILE_TEXT,
                "name": HOSTILE_TEXT,
                "duration_ms": 10.0,
                "model": HOSTILE_TEXT,
                "input_tokens": 1,
                "output_tokens": 2,
                "status": HOSTILE_TEXT,
            }
        ],
    }
    out = export_report_markdown(report, tmp_path / "report.md")
    markdown = out.read_text(encoding="utf-8")

    _assert_prose_cannot_inject_blocks(markdown)
    event_row = next(line for line in markdown.splitlines() if "alpha&#124;beta" in line)
    assert event_row.count("|") == 8
    assert "````code `one` ``two`` ```three``` <script>````" in markdown


def test_plan_and_diagnosis_exports_escape_headings_and_trace_fields(tmp_path) -> None:
    current = {
        "runtime_ms": 1000.0,
        "estimated_cost_usd": 0.1,
        "repeated_context_ratio": 0.0,
        "retry_count": 0,
    }
    after = {
        "runtime_ms": 900.0,
        "estimated_cost_usd": 0.09,
        "latency_reduction_pct": 10.0,
        "cost_reduction_pct": 10.0,
    }
    card = {
        "title": HOSTILE_TEXT,
        "type": HOSTILE_CODE,
        "confidence": HOSTILE_TEXT,
        "why": HOSTILE_TEXT,
        "rewrite_hint": HOSTILE_TEXT,
        "estimated_latency_savings_ms": 100.0,
        "estimated_cost_savings_usd": 0.01,
    }
    plan = {
        "name": HOSTILE_TEXT,
        "run_id": HOSTILE_CODE,
        "current": current,
        "estimated_after": after,
        "optimization_cards": [card],
        "graph": {
            "bottlenecks": [
                {
                    "name": HOSTILE_TEXT,
                    "event_type": HOSTILE_TEXT,
                    "duration_ms": 100.0,
                    "runtime_share": 0.1,
                }
            ]
        },
    }
    plan_path = export_optimization_markdown(plan, tmp_path / "plan.md")
    plan_markdown = plan_path.read_text(encoding="utf-8")

    diagnosis = {
        "name": HOSTILE_TEXT,
        "run_id": HOSTILE_CODE,
        "current": current,
        "estimated_after": after,
        "findings": [
            {
                "finding_id": HOSTILE_TEXT,
                "title": HOSTILE_TEXT,
                "severity": HOSTILE_TEXT,
                "type": HOSTILE_CODE,
                "confidence": HOSTILE_TEXT,
                "affected_spans": [HOSTILE_TEXT],
                "savings": {
                    "estimated_latency_savings_ms": 100.0,
                    "estimated_cost_savings_usd": 0.01,
                },
                "rewrite": {"hint": HOSTILE_TEXT},
                "validation": {"acceptance_criteria": HOSTILE_TEXT},
            }
        ],
    }
    diagnosis_markdown = diagnosis_to_markdown(diagnosis)

    _assert_prose_cannot_inject_blocks(plan_markdown)
    _assert_prose_cannot_inject_blocks(diagnosis_markdown)
    bottleneck_row = next(
        line for line in plan_markdown.splitlines() if line.startswith("| alpha&#124;beta")
    )
    assert bottleneck_row.count("|") == 5
    assert len([line for line in diagnosis_markdown.splitlines() if line.startswith("#")]) == 3


def test_issue_drafts_escape_queue_text_without_changing_github_title() -> None:
    queue = [
        {
            "queue_id": "queue_1",
            "type": HOSTILE_CODE,
            "title": HOSTILE_TEXT,
            "severity": HOSTILE_CODE,
            "occurrence_count": 1,
            "run_count": 1,
            "affected_runs": [HOSTILE_CODE],
            "patchable_count": 1,
            "quality_risk": HOSTILE_CODE,
            "estimated_latency_savings_ms": 100.0,
            "estimated_cost_savings_usd": 0.01,
            "priority_score": 1.0,
        }
    ]
    drafts = build_issue_drafts(queue)
    markdown = issue_drafts_to_markdown(drafts)

    assert HOSTILE_TEXT in drafts[0]["title"]
    assert "## injected" in drafts[0]["title"]
    _assert_prose_cannot_inject_blocks(markdown)
    assert "````code `one` ``two`` ```three``` <script>````" in markdown


def test_patch_plan_uses_dynamic_fences_and_escapes_all_other_fields() -> None:
    plan = {
        "name": HOSTILE_TEXT,
        "run_id": HOSTILE_CODE,
        "dry_run": True,
        "repo_path": HOSTILE_CODE,
        "summary": {
            "patch_count": 1,
            "unsupported_finding_count": 1,
            "frameworks_detected": [HOSTILE_TEXT],
        },
        "patch_plans": [
            {
                "patch_id": HOSTILE_TEXT,
                "finding_id": HOSTILE_CODE,
                "type": HOSTILE_CODE,
                "title": HOSTILE_TEXT,
                "risk": HOSTILE_TEXT,
                "framework": HOSTILE_TEXT,
                "evidence_spans": [HOSTILE_TEXT],
                "before_pattern": HOSTILE_TEXT,
                "proposed_rewrite": HOSTILE_TEXT,
                "suggested_diff": "before\r\n```\n````\n<script>literal</script>",
                "validation_command": HOSTILE_CODE,
                "acceptance_criteria": HOSTILE_TEXT,
                "files": [
                    {
                        "path": HOSTILE_CODE,
                        "symbols": [HOSTILE_TEXT],
                        "confidence": HOSTILE_TEXT,
                        "reason": HOSTILE_TEXT,
                        "locations": [{"symbol": HOSTILE_TEXT, "line": HOSTILE_TEXT}],
                    }
                ],
                "notes": [HOSTILE_TEXT],
            }
        ],
        "unsupported_findings": [
            {"finding_id": HOSTILE_CODE, "type": HOSTILE_CODE, "reason": HOSTILE_TEXT}
        ],
    }
    markdown = patch_plan_to_markdown(plan)

    assert "\n## injected" not in markdown
    assert "`````text\nbefore\n```\n````\n<script>literal</script>\n`````" in markdown
    assert markdown.count("`````") == 2
    assert "&lt;script&gt;&#91;link&#93;" in markdown


def test_ci_quality_and_replay_tables_keep_hostile_values_in_one_cell() -> None:
    ci_markdown = ci_report_to_markdown(
        {
            "summary": {
                "merge_recommendation": HOSTILE_TEXT,
                "finding_count": 1,
                "high_severity_count": 1,
                "patchable_count": 1,
                "latency_improvement_pct": 1.0,
                "cost_improvement_pct": 1.0,
                "retry_count_delta": 0,
                "candidate_schema_validity": None,
                "candidate_quality_score": None,
            },
            "replay": {
                "summary": HOSTILE_TEXT,
                "gates": {
                    "passed": True,
                    "results": [{"name": HOSTILE_TEXT, "passed": True, "detail": HOSTILE_TEXT}],
                },
            },
            "diagnosis": {
                "findings": [
                    {
                        "finding_id": HOSTILE_TEXT,
                        "title": HOSTILE_TEXT,
                        "severity": HOSTILE_TEXT,
                        "type": HOSTILE_TEXT,
                        "rewrite": {"hint": HOSTILE_TEXT},
                    }
                ]
            },
        }
    )
    quality_markdown = quality_report_to_markdown(
        {
            "passed": True,
            "case_count": 1,
            "baseline_score": 1.0,
            "candidate_score": 1.0,
            "quality_delta": 0.0,
            "failed_case_count": 0,
            "cases": [
                {
                    "case_id": HOSTILE_TEXT,
                    "passed": True,
                    "baseline": {"score": 1.0},
                    "candidate": {"score": 1.0, "detail": HOSTILE_TEXT},
                }
            ],
        }
    )
    replay_markdown = replay_report_to_markdown(_replay_report())

    for markdown in (ci_markdown, quality_markdown, replay_markdown):
        _assert_prose_cannot_inject_blocks(markdown)
    _assert_hostile_table_rows_have_columns(ci_markdown, {4, 5})
    _assert_hostile_table_rows_have_columns(quality_markdown, {6})
    _assert_hostile_table_rows_have_columns(replay_markdown, {4})


def _replay_report() -> dict[str, object]:
    baseline = {
        "name": HOSTILE_CODE,
        "run_id": HOSTILE_CODE,
        "runtime_ms": 100.0,
        "estimated_cost_usd": 0.1,
        "input_tokens": 10,
        "output_tokens": 5,
        "retry_count": 0,
        "tool_call_count": 1,
        "model_call_count": 1,
        "schema_validity_pct": None,
        "quality_score": None,
    }
    candidate = dict(baseline)
    deltas = {
        "runtime_ms_delta": 0.0,
        "latency_improvement_pct": 0.0,
        "cost_usd_delta": 0.0,
        "cost_improvement_pct": 0.0,
        "input_tokens_delta": 0,
        "input_token_improvement_pct": 0.0,
        "output_tokens_delta": 0,
        "output_token_improvement_pct": 0.0,
        "retry_count_delta": 0,
        "retry_improvement_pct": 0.0,
        "tool_call_count_delta": 0,
        "tool_call_improvement_pct": 0.0,
        "model_call_count_delta": 0,
        "model_call_improvement_pct": 0.0,
    }
    return {
        "baseline": baseline,
        "candidate": candidate,
        "deltas": deltas,
        "gates": {
            "passed": True,
            "results": [{"name": HOSTILE_TEXT, "passed": True, "detail": HOSTILE_TEXT}],
        },
        "quality": None,
    }


def _assert_prose_cannot_inject_blocks(markdown: str) -> None:
    assert "\r" not in markdown
    assert "\n## injected" not in markdown
    assert "<script>[link]" not in markdown
    assert "&lt;script&gt;&#91;link&#93;" in markdown


def _assert_hostile_table_rows_have_columns(markdown: str, expected_counts: set[int]) -> None:
    rows = [line for line in markdown.splitlines() if "alpha&#124;beta" in line]
    assert rows
    assert {row.count("|") for row in rows} <= expected_counts
