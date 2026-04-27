from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from agentloop import trace_agent, trace_model_call, trace_tool_call
from examples.fake_tools import fake_read, fake_search, sleep_ms

SYSTEM = "You are a careful research agent. Use evidence, verify claims, and write concise reports. " * 4


def main() -> None:
    with trace_agent("research_agent_optimized") as trace:
        with trace_model_call("plan", model="gpt-4.1-mini", input_text=SYSTEM + "Plan research on agent inference runtimes.", output_tokens=120):
            sleep_ms(200)

        with trace_tool_call("search_web", metadata={"query": "agent inference runtime"}):
            sources = fake_search("agent inference runtime")

        with trace_tool_call("read_sources_parallel", metadata={"count": len(sources)}):
            with ThreadPoolExecutor(max_workers=5) as pool:
                contents = list(pool.map(fake_read, sources))

        compact_context = "\n".join(content[:300] for content in contents)
        with trace_model_call("summarize_batch", model="gpt-4.1-mini", input_text=SYSTEM + compact_context, output_tokens=500):
            sleep_ms(300)

        with trace_model_call("verify_and_write", model="gpt-4.1", input_text=SYSTEM + compact_context, output_tokens=650):
            sleep_ms(450)

    trace.export_json("runs/research_agent_optimized.json")
    trace.print_report()


if __name__ == "__main__":
    main()
