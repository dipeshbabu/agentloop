from __future__ import annotations

from agentloop import trace_agent, trace_model_call
from agentloop.integrations.langgraph import trace_node
from examples.fake_tools import sleep_ms


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


def main() -> None:
    with trace_agent("langgraph_style_demo") as trace:
        state = {"question": "How should agent runtimes be profiled?"}
        with trace_model_call("planner", model="gpt-4.1-mini", input_text=state["question"], output_tokens=120):
            sleep_ms(160)
        state = retrieve_sources(state)
        state = rank_sources(state)
        with trace_model_call("final_answer", model="gpt-4.1-mini", input_text=str(state), output_tokens=300):
            sleep_ms(240)
    trace.export_json("runs/langgraph_style_demo.json")
    trace.print_report()


if __name__ == "__main__":
    main()
