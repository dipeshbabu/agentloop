from __future__ import annotations

import copy

import pytest
from workflow_fixtures import pipeline

from agentloop import (
    AgentTrace,
    JudgeIdentity,
    JudgeUsage,
    JudgmentAnswer,
    JudgmentSession,
    JudgmentSpec,
    LocalCallbackJudge,
    attach_judgment,
    build_diagnosis,
    judgment_request,
    read_judgments,
    trace_from_otel,
    trace_to_otel,
)
from agentloop.entrypoint import _analysis_payload
from agentloop.exporters import export_report_markdown
from agentloop.findings import diagnosis_to_markdown
from agentloop.html_report import analysis_to_html
from agentloop.judgment_types import JUDGMENT_KEY
from agentloop.optimizer import build_optimization_plan
from agentloop.store import SQLiteTraceStore


def evaluated_trace():
    trace = pipeline()
    judge = LocalCallbackJudge(
        JudgeIdentity.configured("test-rule", "1", {"mode": "offline"}),
        lambda item, **options: JudgmentAnswer(
            False, usage=JudgeUsage(cost_usd=0, cost_basis="reported")
        ),
    )
    evaluator = JudgmentSession(judge)
    result = evaluator.evaluate(
        judgment_request(trace, JudgmentSpec("Same work?", "boolean"), ["classify", "priority"]),
        enabled=True,
    )
    attach_judgment(trace, result)
    return trace


def test_json_sqlite_and_otel_preserve_judgments_and_cost(tmp_path):
    trace = evaluated_trace()
    trace.export_json(tmp_path / "trace.json")
    store = SQLiteTraceStore(str(tmp_path / "traces.db"))
    store.save_trace(trace)
    restored = [
        AgentTrace.from_json(tmp_path / "trace.json"),
        store.get_trace(trace.run_id),
        trace_from_otel(trace_to_otel(trace)),
    ]
    for item in restored:
        assert item.metadata[JUDGMENT_KEY] == trace.metadata[JUDGMENT_KEY]
        evidence = read_judgments(item)
        assert evidence["status"] == "complete"
        assert evidence["known_cost_usd"] == 0
        assert evidence["records"][0]["effective_value"] is False


def test_ordinary_analysis_never_calls_judge_and_legacy_reports_are_unchanged(monkeypatch):
    trace = pipeline()
    original = trace.report()
    calls = []
    monkeypatch.setattr(LocalCallbackJudge, "judge", lambda *args, **kwargs: calls.append(args))
    assert trace.report() == original
    assert "semantic_judgments" not in original
    assert "evidence_categories" not in original
    assert "semantic_judgments" not in build_optimization_plan(trace)
    assert "semantic_judgments" not in build_diagnosis(trace)
    assert "Offline semantic judgments" not in analysis_to_html(_analysis_payload(trace))
    assert calls == []


def test_saved_evidence_is_visible_but_never_becomes_quality_or_workflow_cost(
    tmp_path, monkeypatch
):
    before = pipeline().report()
    trace = evaluated_trace()

    def forbidden(*args, **kwargs):
        raise AssertionError("analysis cannot run a judge")

    monkeypatch.setattr(LocalCallbackJudge, "judge", forbidden)
    report = trace.report()
    diagnosis = build_diagnosis(trace)
    assert report["semantic_judgments"]["status"] == "complete"
    assert diagnosis["semantic_judgments"] == report["semantic_judgments"]
    assert set(report["evidence_categories"]) == {
        "observed_facts",
        "deterministic_inference",
        "semantic_judgments",
    }
    assert "quality_score" not in report
    for key in (
        "estimated_cost_usd",
        "total_runtime_ms",
        "input_tokens",
        "output_tokens",
        "finding_candidates",
    ):
        assert report[key] == before[key]
    markdown = export_report_markdown(report, tmp_path / "report.md").read_text(encoding="utf-8")
    assert "Offline semantic judgments" in markdown
    assert "False" in markdown and "uncalibrated" in markdown
    assert "Offline semantic judgments" in diagnosis_to_markdown(diagnosis)
    assert "Offline semantic judgments" in analysis_to_html(_analysis_payload(trace))


def test_untrusted_judge_strings_are_escaped_in_html_and_markdown(tmp_path):
    trace = pipeline()
    identity = JudgeIdentity.configured("<script>alert(1)</script>|\n# Judge", "1", {})
    judge = LocalCallbackJudge(identity, lambda item, **options: JudgmentAnswer(True))
    record = JudgmentSession(judge).evaluate(
        judgment_request(trace, JudgmentSpec("Question", "boolean"), ["classify"]), enabled=True
    )
    attach_judgment(trace, record)
    html = analysis_to_html(_analysis_payload(trace))
    assert "<script>" not in html and "&lt;script&gt;" in html
    markdown = export_report_markdown(trace.report(), tmp_path / "report.md").read_text(
        encoding="utf-8"
    )
    assert "<script>" not in markdown and "\n# Judge" not in markdown


def test_read_hashes_each_selected_source_once(monkeypatch):
    trace = evaluated_trace()
    first = trace.metadata[JUDGMENT_KEY]["records"][0]
    # Real distinct cache receipts share evidence. This is a complexity assertion
    # on expensive source hashing, independent of the number of receipts.
    judge = LocalCallbackJudge(
        JudgeIdentity(**first["judge"]), lambda item, **options: JudgmentAnswer(True)
    )
    evaluator = JudgmentSession(judge)
    item = judgment_request(trace, JudgmentSpec("Question", "boolean"), ["classify", "priority"])
    for _ in range(20):
        attach_judgment(trace, evaluator.evaluate(item, enabled=True))
    import agentloop.judgments as module

    original = module._event_hash
    calls = []

    def hashed(event):
        calls.append(event.event_id)
        return original(event)

    monkeypatch.setattr(module, "_event_hash", hashed)
    assert read_judgments(trace)["status"] == "complete"
    assert sorted(calls) == ["classify", "priority"]


@pytest.mark.parametrize(
    "artifact",
    [
        None,
        [],
        {},
        {"schema_version": "1.0", "records": None},
        {"schema_version": "1.0", "records": [None]},
    ],
)
def test_malformed_metadata_is_explicit_without_crashing_analysis(artifact):
    trace = pipeline()
    trace.metadata[JUDGMENT_KEY] = copy.deepcopy(artifact)
    result = trace.report()
    assert result["semantic_judgments"]["status"] in {"invalid", "unsupported"}


def test_otel_content_redaction_retains_receipt_but_invalidates_answer():
    trace = pipeline()
    trace.events[0].input_text = "private source content"
    judge = LocalCallbackJudge(
        JudgeIdentity.configured("test", "1", {}), lambda item, **options: JudgmentAnswer(True)
    )
    record = JudgmentSession(judge).evaluate(
        judgment_request(trace, JudgmentSpec("Question", "boolean"), ["classify"]), enabled=True
    )
    attach_judgment(trace, record)
    restored = trace_from_otel(trace_to_otel(trace))
    assert restored.metadata[JUDGMENT_KEY] == trace.metadata[JUDGMENT_KEY]
    assert restored.events[0].input_text is None
    assert read_judgments(restored)["records"][0]["effective_status"] == "stale"
