from __future__ import annotations

from agentloop.exporters import export_report_markdown
from agentloop.tracer import trace_agent, trace_model_call


def test_export_report_markdown(tmp_path) -> None:
    with trace_agent("export-test") as trace:
        with trace_model_call("call", input_tokens=10, output_tokens=2):
            pass
    out = export_report_markdown(trace.report(), tmp_path / "report.md")
    assert out.exists()
    assert "AgentLoop Report" in out.read_text(encoding="utf-8")
