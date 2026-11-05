from __future__ import annotations

import asyncio

import pytest

from agentloop.tracer import (
    current_trace,
    trace_agent,
    trace_model_call,
    trace_retry,
    trace_tool_call,
)


@pytest.mark.parametrize(
    "failure",
    [ValueError("retry failed"), KeyboardInterrupt(), SystemExit(1), asyncio.CancelledError()],
)
def test_retry_records_failures_and_preserves_the_original_exception(failure) -> None:
    with trace_agent("retry-failure") as trace:
        with pytest.raises(type(failure)) as caught:
            with trace_retry("attempt"):
                raise failure
    assert caught.value is failure
    assert trace.events[0].status == "error"
    assert trace.events[0].error
    assert current_trace() is None


@pytest.mark.parametrize("scope", [trace_model_call, trace_tool_call, trace_retry])
def test_broken_exception_formatting_cannot_mask_the_business_failure(scope) -> None:
    class BrokenMessageError(Exception):
        def __str__(self):
            raise RuntimeError("exception formatter failed")

    failure = BrokenMessageError()
    with trace_agent("broken-exception") as trace:
        with pytest.raises(BrokenMessageError) as caught:
            with scope("call"):
                raise failure
    assert caught.value is failure
    assert trace.events[0].status == "error"
    assert trace.events[0].error == "BrokenMessageError"
    assert current_trace() is None
