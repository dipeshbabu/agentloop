"""Per-invocation usage observation that never retains a raw result or chunk."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from agentloop.budget_types import ResourceUsage


class UsageTracker:
    def __init__(
        self, reader: Callable[[Any], ResourceUsage | None] | None, invoke: Callable
    ) -> None:
        self.reader = reader
        self.invoke = invoke
        self.latest: ResourceUsage | None = None
        self.error: str | None = None

    def observe(self, value: Any) -> None:
        if self.reader is None:
            return
        try:
            report = self.invoke(self.reader, value)
            if report is None:
                return
            if type(report) is not ResourceUsage:
                if inspect.iscoroutine(report):
                    report.close()
                self.error = "invalid_usage"
                return
        except Exception:
            self.error = "collector_error"
            return
        except BaseException:
            self.error = "collector_cancelled"
            raise
        # Usage updates are snapshots, not increments. In particular, successive
        # cumulative stream usage chunks must never be added to one another.
        self.latest = report
        self.error = None


class ObservedGenerator:
    """Delegate the complete generator protocol while observing returned chunks."""

    def __init__(self, source: Any, tracker: UsageTracker) -> None:
        self.source = source
        self.tracker = tracker

    def __iter__(self):
        return self

    def _advance(self, operation: Callable, *args: Any):
        try:
            value = operation(*args)
        except StopIteration as stopped:
            if stopped.value is not None:
                self.tracker.observe(stopped.value)
            raise
        self.tracker.observe(value)
        return value

    def __next__(self):
        return self._advance(self.source.__next__)

    def send(self, value):
        return self._advance(self.source.send, value)

    def throw(self, *args):
        if len(args) == 3 and isinstance(args[1], BaseException):
            return self._advance(self.source.throw, args[1].with_traceback(args[2]))
        return self._advance(self.source.throw, *args)

    def close(self):
        return self.source.close()
