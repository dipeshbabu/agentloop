from __future__ import annotations

import json
import time
from pathlib import Path

import agentloop


@agentloop.trace_model(name="planner_llm", model="demo-llm")
def planner(question: str) -> str:
    time.sleep(0.02)
    return f"Plan for: {question}"


@agentloop.trace_tool(name="search_docs")
def search_docs(query: str) -> list[str]:
    time.sleep(0.01)
    return [f"doc about {query}", "pricing page", "integration guide"]


@agentloop.trace_tool(name="write_answer")
def write_answer(plan: str, docs: list[str]) -> str:
    time.sleep(0.01)
    return f"{plan}\nSources: {', '.join(docs[:2])}"


@agentloop.traceable(root=True, agent_name="decorator_research_agent")
def run_agent(question: str) -> str:
    plan = planner(question)
    docs = search_docs(question)
    return write_answer(plan, docs)


def main() -> None:
    agentloop.init(export_dir="runs", auto_store=False, auto_upload=False)
    answer = run_agent("How do I reduce agent latency?")
    # The trace has already finalized after run_agent exits, so load latest JSON for display.
    latest = max(Path("runs").glob("run_*.json"), key=lambda path: path.stat().st_mtime)
    print(answer)
    print(f"\nWrote trace: {latest}")
    print(json.dumps(agentloop.AgentTrace.from_json(latest).report(), indent=2))


if __name__ == "__main__":
    main()
