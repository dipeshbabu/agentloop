from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agentloop import trace_agent, trace_model_call, trace_retry, trace_tool_call

SYSTEM_LONG = "You are a careful research agent. Use evidence, verify claims, and write concise reports. " * 20
SYSTEM_SHORT = "You are a careful research agent. Use evidence, verify claims, and write concise reports. " * 4


def sleep_ms(ms: int) -> None:
    time.sleep(ms / 1000)


def fake_search(query: str) -> list[str]:
    sleep_ms(300)
    return [f"source-{i}-{query}" for i in range(1, 6)]


def fake_read(source: str) -> str:
    sleep_ms(250)
    return f"Content from {source}. " * 30


def run_baseline(out_dir: str | Path = "runs") -> Path:
    out_path = Path(out_dir) / "research_agent_baseline.json"
    with trace_agent("research_agent_baseline") as trace:
        with trace_model_call(
            "plan",
            model="gpt-4.1",
            input_text=SYSTEM_LONG + "Plan research on agent inference runtimes.",
            output_tokens=220,
        ):
            sleep_ms(400)

        with trace_tool_call("search_web", metadata={"query": "agent inference runtime"}):
            sources = fake_search("agent inference runtime")

        summaries = []
        for source in sources:
            with trace_tool_call("read_source", metadata={"source": source}):
                content = fake_read(source)
            with trace_model_call(
                "summarize_source",
                model="gpt-4.1",
                input_text=SYSTEM_LONG + content,
                output_tokens=350,
            ):
                sleep_ms(350)
                summaries.append("summary for " + source)

        with trace_retry("parse_failed_then_retry", metadata={"reason": "invalid json"}):
            sleep_ms(250)

        with trace_model_call(
            "verify_claims",
            model="gpt-4.1",
            input_text=SYSTEM_LONG + "\n".join(summaries),
            output_tokens=450,
        ):
            sleep_ms(450)

        with trace_model_call(
            "write_report",
            model="gpt-4.1",
            input_text=SYSTEM_LONG + "\n".join(summaries),
            output_tokens=800,
        ):
            sleep_ms(550)

    trace.export_json(out_path)
    trace.print_report()
    return out_path


def run_optimized(out_dir: str | Path = "runs") -> Path:
    out_path = Path(out_dir) / "research_agent_optimized.json"
    with trace_agent("research_agent_optimized") as trace:
        with trace_model_call(
            "plan",
            model="gpt-4.1-mini",
            input_text=SYSTEM_SHORT + "Plan research on agent inference runtimes.",
            output_tokens=120,
        ):
            sleep_ms(200)

        with trace_tool_call("search_web", metadata={"query": "agent inference runtime"}):
            sources = fake_search("agent inference runtime")

        with trace_tool_call("read_sources_parallel", metadata={"count": len(sources)}):
            with ThreadPoolExecutor(max_workers=5) as pool:
                contents = list(pool.map(fake_read, sources))

        compact_context = "\n".join(content[:300] for content in contents)
        with trace_model_call(
            "summarize_batch",
            model="gpt-4.1-mini",
            input_text=SYSTEM_SHORT + compact_context,
            output_tokens=500,
        ):
            sleep_ms(300)

        with trace_model_call(
            "verify_and_write",
            model="gpt-4.1",
            input_text=SYSTEM_SHORT + compact_context,
            output_tokens=650,
        ):
            sleep_ms(450)

    trace.export_json(out_path)
    trace.print_report()
    return out_path


def run_langgraph_style(out_dir: str | Path = "runs") -> Path:
    from agentloop.integrations.langgraph import trace_node

    @trace_node("retrieve_sources")
    def retrieve_sources(state: dict) -> dict:
        sleep_ms(250)
        state["sources"] = ["paper-a", "paper-b", "paper-c"]
        return state

    @trace_node("rank_sources")
    def rank_sources(state: dict) -> dict:
        sleep_ms(180)
        state["sources"] = state["sources"][:2]
        return state

    out_path = Path(out_dir) / "langgraph_style_demo.json"
    with trace_agent("langgraph_style_demo") as trace:
        state = {"question": "How should agent runtimes be profiled?"}
        with trace_model_call("planner", model="gpt-4.1-mini", input_text=state["question"], output_tokens=120):
            sleep_ms(160)
        state = retrieve_sources(state)
        state = rank_sources(state)
        with trace_model_call("final_answer", model="gpt-4.1-mini", input_text=str(state), output_tokens=300):
            sleep_ms(240)
    trace.export_json(out_path)
    trace.print_report()
    return out_path
