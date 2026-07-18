from pathlib import Path

from agentloop.issues import build_issue_drafts, issue_drafts_to_markdown

ROOT = Path(__file__).resolve().parents[1]


def test_build_issue_drafts_for_patchable_queue_items() -> None:
    queue = [
        {
            "queue_id": "queue_1",
            "type": "cache_context",
            "title": "Cache repeated prompt/context prefix",
            "severity": "high",
            "occurrence_count": 3,
            "run_count": 2,
            "affected_runs": ["run_a", "run_b"],
            "patchable_count": 3,
            "estimated_latency_savings_ms": 1200,
            "estimated_cost_savings_usd": 0.42,
            "priority_score": 1234.5,
        }
    ]

    drafts = build_issue_drafts(queue)
    markdown = issue_drafts_to_markdown(drafts)

    assert drafts[0]["title"].startswith("Optimize agent workflow")
    assert drafts[0]["labels"] == ["enhancement"]
    assert "`run_a`" in drafts[0]["body"]
    assert "AgentLoop GitHub Issue Drafts" in markdown


def test_issue_forms_reference_only_existing_primary_labels() -> None:
    expected = {
        "bug_report.yml": "labels: [bug]",
        "feature_request.yml": "labels: [enhancement]",
        "question.yml": "labels: [question]",
    }

    forms = ROOT / ".github" / "ISSUE_TEMPLATE"
    for filename, label_line in expected.items():
        content = (forms / filename).read_text(encoding="utf-8")
        assert label_line in content
        assert "triage" not in content
