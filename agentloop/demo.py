from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agentloop import trace_agent, trace_model_call, trace_retry, trace_tool_call

SYSTEM_LONG = (
    "You are a careful research agent. Use evidence, verify claims, and write concise reports. "
    * 20
)
SYSTEM_SHORT = (
    "You are a careful research agent. Use evidence, verify claims, and write concise reports. " * 4
)


def sleep_ms(ms: int) -> None:
    time.sleep(ms / 1000)


def fake_search(query: str) -> list[str]:
    sleep_ms(300)
    return [f"source-{i}-{query}" for i in range(1, 6)]


def fake_read(source: str) -> str:
    sleep_ms(250)
    return f"Content from {source}. " * 30


def inspect_result(result: str) -> str:
    sleep_ms(120)
    return f"inspected {result}"


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


def run_proof_pair(out_dir: str | Path = "runs") -> tuple[Path, Path]:
    """Generate a before/after trace pair for the closed-loop product demo."""

    return run_proof_baseline(out_dir), run_proof_candidate(out_dir)


def run_proof_baseline(out_dir: str | Path = "runs") -> Path:
    out_path = Path(out_dir) / "agentloop_proof_baseline.json"
    repeated_context = SYSTEM_LONG + "Classify and summarize every source independently. " * 20
    with trace_agent(
        "agentloop_proof_baseline", metadata={"schema_valid": False, "quality_score": 0.82}
    ) as trace:
        with trace_model_call(
            "classify_intent",
            model="gpt-4.1",
            input_text="Classify this simple support request.",
            output_tokens=40,
        ):
            sleep_ms(220)

        with trace_tool_call("search_web", metadata={"query": "agent workflow profiler"}):
            sources = fake_search("agent workflow profiler")[:3]

        summaries = []
        for source in sources:
            with trace_tool_call("fake_read", metadata={"source": source}):
                fake_read(source)

        for index in range(15):
            with trace_model_call(
                "summarize_source",
                model="gpt-4.1-mini",
                input_text=repeated_context + f" item {index}",
                input_tokens=80,
                output_tokens=60,
            ):
                sleep_ms(12)
                summaries.append(f"summary {index}")

        for index in range(8):
            with trace_tool_call("plan_next", metadata={"iteration": index}):
                sleep_ms(60)

        for name in ["search_web", "inspect_result", "search_web", "inspect_result"]:
            with trace_tool_call(name):
                if name == "search_web":
                    fake_search("same query")
                else:
                    inspect_result("same result")

        with trace_retry("invalid_json_retry", metadata={"reason": "schema_validation_failed"}):
            sleep_ms(180)

        large_context = repeated_context + "\n".join(summaries) + (" Evidence chunk. " * 1600)
        with trace_model_call(
            "final_answer",
            model="gpt-4.1",
            input_text=large_context,
            input_tokens=4000,
            output_tokens=700,
        ):
            sleep_ms(500)

    trace.export_json(out_path)
    trace.print_report()
    return out_path


def run_proof_candidate(out_dir: str | Path = "runs") -> Path:
    out_path = Path(out_dir) / "agentloop_proof_candidate.json"
    compact_context = (
        SYSTEM_SHORT + "Use cached instructions, batch summaries, and stop after convergence."
    )
    with trace_agent(
        "agentloop_proof_candidate", metadata={"schema_valid": True, "quality_score": 0.94}
    ) as trace:
        with trace_model_call(
            "classify_intent",
            model="gpt-4.1-mini",
            input_text="Classify this simple support request.",
            output_tokens=30,
        ):
            sleep_ms(90)

        with trace_tool_call("search_web", metadata={"query": "agent workflow profiler"}):
            sources = fake_search("agent workflow profiler")[:3]

        with trace_tool_call("fake_read_parallel", metadata={"count": len(sources)}):
            with ThreadPoolExecutor(max_workers=3) as pool:
                contents = list(pool.map(fake_read, sources))

        with trace_model_call(
            "summarize_batch",
            model="gpt-4.1-mini",
            input_text=compact_context + "\n".join(content[:220] for content in contents),
            output_tokens=280,
        ):
            sleep_ms(170)

        with trace_tool_call(
            "plan_next", metadata={"iteration": 1, "stop_reason": "state_converged"}
        ):
            sleep_ms(60)

        with trace_model_call(
            "final_answer",
            model="gpt-4.1-mini",
            input_text=compact_context + " Compressed evidence with source ids.",
            output_tokens=420,
        ):
            sleep_ms(220)

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
        with trace_model_call(
            "planner", model="gpt-4.1-mini", input_text=state["question"], output_tokens=120
        ):
            sleep_ms(160)
        state = retrieve_sources(state)
        state = rank_sources(state)
        with trace_model_call(
            "final_answer", model="gpt-4.1-mini", input_text=str(state), output_tokens=300
        ):
            sleep_ms(240)
    trace.export_json(out_path)
    trace.print_report()
    return out_path
