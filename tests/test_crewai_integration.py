from __future__ import annotations

import asyncio

import pytest

from agentloop.integrations.crewai import instrument_agent, instrument_crew, instrument_task
from agentloop.tracer import trace_agent


class FakeTask:
    name = "task"

    def execute(self) -> str:
        return "done"


class FakeAgent:
    role = "researcher"

    def execute_task(self, task: str) -> str:
        return f"executed {task}"


class FakeCrew:
    name = "crew"

    def kickoff(self) -> str:
        return "started"

    async def kickoff_async(self) -> str:
        return "started_async"


def test_instrument_task_records_task_span() -> None:
    task = instrument_task(FakeTask())
    with trace_agent("task_test") as trace:
        assert task.execute() == "done"
    assert len(trace.events) == 1
    assert trace.events[0].name == "crewai.task.execute"
    assert trace.events[0].metadata["integration"] == "crewai"


def test_instrument_agent_records_agent_span() -> None:
    agent = instrument_agent(FakeAgent())
    with trace_agent("agent_test") as trace:
        assert agent.execute_task("draft") == "executed draft"
    assert len(trace.events) == 1
    assert trace.events[0].name == "crewai.agent.execute_task"
    assert trace.events[0].metadata["agent_name"] == "researcher"


def test_instrument_crew_wraps_kickoff_with_trace() -> None:
    crew = instrument_crew(FakeCrew())
    assert crew.kickoff() == "started"


def test_instrument_crew_wraps_async_kickoff_with_trace() -> None:
    crew = instrument_crew(FakeCrew())

    async def run() -> None:
        assert await crew.kickoff_async() == "started_async"

    asyncio.run(run())


def test_instrument_task_records_async_cancellation_once_and_propagates() -> None:
    cancellation = asyncio.CancelledError()

    class CancelledTask:
        description = "cancelled"

        async def execute_async(self) -> None:
            raise cancellation

    task = instrument_task(CancelledTask())

    async def run() -> None:
        with trace_agent("cancelled_task") as trace:
            with pytest.raises(asyncio.CancelledError) as caught:
                await task.execute_async()
        assert caught.value is cancellation
        assert len(trace.events) == 1
        assert trace.events[0].name == "crewai.task.execute_async"
        assert trace.events[0].status == "error"
        assert trace.events[0].error == "CancelledError"

    asyncio.run(run())
