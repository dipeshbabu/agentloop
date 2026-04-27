from __future__ import annotations

from agentloop import trace_agent, trace_model_call, trace_retry, trace_tool_call
from examples.fake_tools import fake_read, fake_search, sleep_ms

SYSTEM = "You are a careful research agent. Use evidence, verify claims, and write concise reports. " * 20


def main() -> None:
    with trace_agent("research_agent_baseline") as trace:
        with trace_model_call("plan", model="gpt-4.1", input_text=SYSTEM + "Plan research on agent inference runtimes.", output_tokens=220):
            sleep_ms(400)

        with trace_tool_call("search_web", metadata={"query": "agent inference runtime"}):
            sources = fake_search("agent inference runtime")

        summaries = []
        for source in sources:
            with trace_tool_call("read_source", metadata={"source": source}):
                content = fake_read(source)
            with trace_model_call("summarize_source", model="gpt-4.1", input_text=SYSTEM + content, output_tokens=350):
                sleep_ms(350)
                summaries.append("summary for " + source)

        with trace_retry("parse_failed_then_retry", metadata={"reason": "invalid json"}):
            sleep_ms(250)

        with trace_model_call("verify_claims", model="gpt-4.1", input_text=SYSTEM + "\n".join(summaries), output_tokens=450):
            sleep_ms(450)

        with trace_model_call("write_report", model="gpt-4.1", input_text=SYSTEM + "\n".join(summaries), output_tokens=800):
            sleep_ms(550)

    trace.export_json("runs/research_agent_baseline.json")
    trace.print_report()


if __name__ == "__main__":
    main()
