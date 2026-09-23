"""Synthetic, offline demonstration of typed judgment receipts and cache costs."""

from __future__ import annotations

import argparse
from pathlib import Path

from agentloop import (
    AgentTrace,
    JudgeIdentity,
    JudgeUsage,
    JudgmentAnswer,
    JudgmentSession,
    JudgmentSpec,
    LocalCallbackJudge,
    attach_judgment,
    judgment_request,
    record_operation,
)
from agentloop.entrypoint import _analysis_payload
from agentloop.exporters import export_report_json, export_report_markdown
from agentloop.html_report import analysis_to_html


def compare_summaries(request, *, timeout_s):
    summaries = [item.summary for item in request.evidence]
    usage = JudgeUsage(0, 0, 0, "reported", "reported")
    if any(item is None for item in summaries):
        return JudgmentAnswer(status="unknown", reason="insufficient_evidence", usage=usage)
    return JudgmentAnswer(summaries[0] == summaries[1], usage=usage)


def main(out):
    trace = AgentTrace(
        "synthetic semantic judgment",
        run_id="synthetic-judgments",
        started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:00:00.020000+00:00",
        elapsed_ms=20,
        metadata={"synthetic": True, "source": "agentloop_semantic_judgment_example"},
    )
    for span_id in ("first", "second"):
        record_operation(
            "synthetic classifier",
            kind="classifier",
            duration_ms=10,
            trace=trace,
            event_id=span_id,
            started_at=trace.started_at,
            ended_at=trace.ended_at,
        )
    identity = JudgeIdentity.configured(
        "example.summary-equality", "1", {"comparison": "exact"}, model_or_rule="summary-equality"
    )
    session = JudgmentSession(LocalCallbackJudge(identity, compare_summaries))
    spec = JudgmentSpec("Do these approved summaries match?", "boolean")
    item = judgment_request(
        trace,
        spec,
        ["first", "second"],
        summaries={"first": "same derived label", "second": "same derived label"},
    )
    for _ in range(2):
        attach_judgment(trace, session.evaluate(item, enabled=True, timeout_s=1))
    missing = judgment_request(trace, spec, ["first", "second"])
    attach_judgment(trace, session.evaluate(missing, enabled=True, timeout_s=1))
    out.mkdir(parents=True, exist_ok=True)
    trace.export_json(out / "trace.json")
    report = trace.report()
    export_report_json(report, out / "report.json")
    export_report_markdown(report, out / "report.md")
    (out / "report.html").write_text(analysis_to_html(_analysis_payload(trace)), encoding="utf-8")
    evidence = report["semantic_judgments"]
    assert [item["effective_status"] for item in evidence["records"]] == [
        "known",
        "known",
        "unknown",
    ]
    assert evidence["records"][1]["invocation"]["cache_hit"]
    assert evidence["known_cost_usd"] == 0 and evidence["cost_complete"]
    print(f"Wrote synthetic judgment, cache-hit and unknown evidence to {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/semantic-judgments"))
    main(parser.parse_args().out)
