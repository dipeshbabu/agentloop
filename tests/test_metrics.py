from __future__ import annotations

from agentloop.tracer import trace_agent, trace_model_call, trace_tool_call


def test_report_has_basic_metrics() -> None:
    with trace_agent("test") as trace:
        with trace_model_call("m", model="gpt-4.1-mini", input_tokens=100, output_tokens=20):
            pass
        with trace_tool_call("t"):
            pass

    report = trace.report()
    assert report["model_call_count"] == 1
    assert report["tool_call_count"] == 1
    assert report["input_tokens"] == 100
    assert report["output_tokens"] == 20
    assert report["estimated_cost_usd"] > 0


def test_repeated_context_recommendation() -> None:
    repeated = "same stable instruction " * 100
    with trace_agent("test") as trace:
        with trace_model_call("a", input_text=repeated, output_tokens=1):
            pass
        with trace_model_call("b", input_text=repeated, output_tokens=1):
            pass

    titles = [rec["title"] for rec in trace.report()["recommendations"]]
    assert "Cache repeated context" in titles
