from __future__ import annotations

from pathlib import Path
from typing import Any

from agentloop.integrations.langgraph import instrument_state_graph, trace_runnable


class FakeStateGraph:
    """Tiny LangGraph-like builder used so this demo has no LangGraph dependency."""

    def __init__(self) -> None:
        self.nodes: list[tuple[str, Any]] = []

    def add_node(self, name: str, action: Any) -> None:
        self.nodes.append((name, action))

    def compile(self) -> "FakeCompiledGraph":
        return FakeCompiledGraph(self.nodes)


class FakeCompiledGraph:
    def __init__(self, nodes: list[tuple[str, Any]]) -> None:
        self.nodes = nodes

    def invoke(self, state: dict[str, Any]) -> dict[str, Any]:
        for _, node in self.nodes:
            state = node(state)
        return state


def retrieve(state: dict[str, Any]) -> dict[str, Any]:
    state["sources"] = ["paper-a", "paper-b", "paper-c"]
    return state


def synthesize(state: dict[str, Any]) -> dict[str, Any]:
    state["answer"] = f"Used {len(state['sources'])} sources."
    return state


def main() -> None:
    builder = FakeStateGraph()
    instrument_state_graph(builder, metadata={"demo": True})
    builder.add_node("retrieve", retrieve)
    builder.add_node("synthesize", synthesize)

    app = trace_runnable(builder.compile(), name="langgraph_auto_demo")
    result = app.invoke({"question": "How do I profile an agent graph?"})
    print(result)

    out = Path("runs/langgraph_auto_demo.json")
    app.export_last_trace(str(out))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
