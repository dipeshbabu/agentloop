from __future__ import annotations

from typing import Any


def instrument(obj: Any, *, kind: str | None = None, name: str | None = None) -> Any:
    """Best-effort one-line instrumentation for common agent objects.

    Explicit `kind` values:
    - `openai`
    - `crewai_crew`
    - `crewai_agent`
    - `crewai_task`
    - `langgraph_state_graph`
    - `langgraph_runnable`

    Without `kind`, AgentLoop uses duck-typing to choose the safest wrapper.
    """

    normalized = kind.lower().replace("-", "_") if kind else None

    if normalized == "openai" or _looks_like_openai_client(obj):
        from agentloop.integrations.openai import instrument_openai_client

        return instrument_openai_client(obj)

    if normalized == "crewai_crew" or _looks_like_crewai_crew(obj):
        from agentloop.integrations.crewai import instrument_crew

        return instrument_crew(obj, name=name)

    if normalized == "crewai_agent":
        from agentloop.integrations.crewai import instrument_agent

        return instrument_agent(obj, name=name)

    if normalized == "crewai_task":
        from agentloop.integrations.crewai import instrument_task

        return instrument_task(obj, name=name)

    if normalized == "langgraph_state_graph" or _looks_like_state_graph(obj):
        from agentloop.integrations.langgraph import instrument_state_graph

        return instrument_state_graph(obj)

    if normalized == "langgraph_runnable" or _looks_like_runnable(obj):
        from agentloop.integrations.langgraph import trace_runnable

        return trace_runnable(
            obj, name=name or getattr(obj, "name", None) or obj.__class__.__name__
        )

    raise ValueError(
        "Could not infer object type for AgentLoop instrumentation. "
        "Pass kind='openai', 'crewai_crew', 'crewai_agent', 'crewai_task', "
        "'langgraph_state_graph', or 'langgraph_runnable'."
    )


def _looks_like_openai_client(obj: Any) -> bool:
    responses = getattr(obj, "responses", None)
    chat = getattr(obj, "chat", None)
    completions = getattr(chat, "completions", None) if chat is not None else None
    return bool(
        responses is not None
        and callable(getattr(responses, "create", None))
        or completions is not None
        and callable(getattr(completions, "create", None))
    )


def _looks_like_crewai_crew(obj: Any) -> bool:
    return callable(getattr(obj, "kickoff", None)) or callable(getattr(obj, "kickoff_async", None))


def _looks_like_state_graph(obj: Any) -> bool:
    return callable(getattr(obj, "add_node", None)) and callable(getattr(obj, "compile", None))


def _looks_like_runnable(obj: Any) -> bool:
    return callable(getattr(obj, "invoke", None)) or callable(getattr(obj, "ainvoke", None))
