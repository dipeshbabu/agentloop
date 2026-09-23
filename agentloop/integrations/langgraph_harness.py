"""Explicit LangGraph dispatch controls; the SDK remains an optional dependency."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import lru_cache, wraps
from importlib.metadata import PackageNotFoundError, version
from weakref import ReferenceType, ref

from agentloop.budget_types import DispatchOptions
from agentloop.harness import (
    ACTIONS,
    BOUNDARIES,
    EXECUTION_KINDS,
    AdapterCapabilities,
    Harness,
    HarnessConfig,
    HarnessControlError,
    HarnessRun,
    Hook,
)
from agentloop.harness_evidence import HarnessEvidenceError
from agentloop.loop_types import StepInfo
from agentloop.tracer import AgentTrace, bind_trace_context, current_event_id, current_trace

SUPPORTED_VERSION = "1.2.11"


def _check_version() -> None:
    try:
        installed = version("langgraph")
    except PackageNotFoundError:
        installed = None
    if installed != SUPPORTED_VERSION:
        raise RuntimeError(f"LangGraph harness requires langgraph=={SUPPORTED_VERSION}")


@lru_cache(maxsize=1)
def _signal_type():
    # Public control-flow base: the pinned runtime bypasses retries and handlers.
    from langgraph.errors import GraphBubbleUp

    class HarnessSignal(GraphBubbleUp):
        def __init__(self, original):
            self.original = original
            super().__init__("AgentLoop harness control")

    return HarnessSignal


def _runtime_identity() -> tuple[str, int]:
    from langgraph.runtime import get_runtime

    info = get_runtime().execution_info
    task_id, attempt = info.task_id, info.node_attempt
    if not isinstance(task_id, str) or not task_id or type(attempt) is not int or attempt < 1:
        raise RuntimeError("LangGraph did not supply a valid task identity and attempt")
    return task_id, attempt


def _kind(function):
    for kind, check in (
        ("async_generator", inspect.isasyncgenfunction),
        ("generator", inspect.isgeneratorfunction),
        ("async", inspect.iscoroutinefunction),
    ):
        if check(function) or check(getattr(function, "__call__", None)):
            return kind
    return "sync"


@dataclass(frozen=True)
class _Session:
    run: HarnessRun
    trace: AgentTrace | None
    parent: str | None


class _ScopedIterator:
    """Forward the generator protocol with no context binding across yields."""

    def __init__(self, source, scope):
        self.source, self.scope = source, scope

    def __iter__(self):
        return self

    def __next__(self):
        with self.scope():
            return next(self.source)

    def send(self, value):
        with self.scope():
            return self.source.send(value)

    def throw(self, *args):
        with self.scope():
            return self.source.throw(*args)

    def close(self):
        with self.scope():
            close = getattr(self.source, "close", None)
            if close is not None:
                return close()


class _ScopedAsyncIterator:
    def __init__(self, source, scope):
        self.source, self.scope = source, scope

    async def __anext__(self):
        with self.scope():
            return await self.source.__anext__()

    async def asend(self, value):
        with self.scope():
            return await self.source.asend(value)

    async def athrow(self, error):
        with self.scope():
            return await self.source.athrow(error)

    async def aclose(self):
        with self.scope():
            return await self.source.aclose()


def _identity(value):
    return value


class LangGraphHarness:
    """Register explicit boundaries before compiling, then wrap the compiled app.

    ``boundaries`` declares coverage, not discovery: only decorated callables
    are controlled. Node admission uses the ``iteration`` boundary. No builder
    registry, provider client, or existing tracing adapter is modified.
    """

    def __init__(
        self,
        config: HarnessConfig = HarnessConfig(),
        *,
        boundaries=frozenset({"iteration", "completion"}),
    ):
        boundaries = frozenset(boundaries)
        if not boundaries <= BOUNDARIES:
            raise ValueError("unsupported LangGraph harness boundary")
        self._harness = Harness(
            config,
            AdapterCapabilities(
                "langgraph",
                frozenset(
                    Hook(boundary, phase)
                    for boundary in boundaries
                    for phase in ("before", "after")
                ),
                ACTIONS,
                EXECUTION_KINDS,
            ),
        )
        self._session: ContextVar[_Session | None] = ContextVar(
            "langgraph_harness_session", default=None
        )
        self._branch: ContextVar[str] = ContextVar("langgraph_harness_branch", default="main")
        self._last_run: ContextVar[HarnessRun | None] = ContextVar(
            "langgraph_harness_last_run", default=None
        )

    @property
    def capabilities(self) -> AdapterCapabilities:
        return self._harness.capabilities

    @property
    def last_run(self) -> HarnessRun | None:
        """Most recently started run in this caller's context, including failures."""
        return self._last_run.get()

    @contextmanager
    def _scope(self, session, branch=None, *, root=False) -> Iterator[None]:
        session_token = self._session.set(session)
        branch_token = self._branch.set(branch if branch is not None else self._branch.get())
        trace_scope = bind_trace_context(session.trace, session.parent) if root else nullcontext()
        try:
            with trace_scope:
                try:
                    yield
                except (HarnessControlError, HarnessEvidenceError) as exc:
                    if root:
                        raise
                    raise _signal_type()(exc) from None
                except _signal_type() as exc:
                    if root:
                        raise exc.original from None
                    raise
        finally:
            self._branch.reset(branch_token)
            self._session.reset(session_token)

    def _start(self):
        session = _Session(self._harness.start_run(), current_trace(), current_event_id())
        self._last_run.set(session.run)
        return session

    def node(self, name: str, *, branch_id: str | None = None, step_reader: Callable | None = None):
        """Decorate a sync/async node before add_node; arguments are passed unchanged.

        By default each framework task is a separate branch. Supply a stable
        branch_id to compare sequential graph iterations. An optional trusted
        step_reader accepts the same arguments and returns bounded StepInfo.
        """
        StepInfo(name)
        if branch_id is not None and (not isinstance(branch_id, str) or not branch_id):
            raise ValueError("branch_id must be a nonempty string")
        if step_reader is not None and (not callable(step_reader) or _kind(step_reader) != "sync"):
            raise ValueError("step_reader must be a synchronous callable")
        return lambda function: self._wrap(
            function,
            "iteration",
            DispatchOptions(step=StepInfo(name)),
            None,
            node=True,
            branch_id=branch_id,
            step_reader=step_reader,
        )

    def model(self, function, *, dispatch: DispatchOptions | None = None, usage_reader=None):
        """Wrap one explicit model callable, including a lazy stream callable."""
        return self._wrap(
            function, "model", DispatchOptions() if dispatch is None else dispatch, usage_reader
        )

    def tool(self, function, *, dispatch: DispatchOptions | None = None, usage_reader=None):
        """Wrap one explicit tool callable; hidden work is outside this boundary."""
        return self._wrap(
            function, "tool", DispatchOptions() if dispatch is None else dispatch, usage_reader
        )

    def _wrap(
        self,
        function,
        boundary,
        options,
        usage_reader,
        *,
        node=False,
        branch_id=None,
        step_reader=None,
    ):
        if not callable(function):
            raise ValueError("protected work must be callable")
        if self._harness.config.mode == "disabled":
            return function
        if Hook(boundary) not in self.capabilities.hooks:
            raise ValueError("boundary must be declared in LangGraphHarness(boundaries=...)")
        if type(options) is not DispatchOptions:
            raise ValueError("dispatch must be DispatchOptions")
        if usage_reader is not None and (
            not callable(usage_reader) or _kind(usage_reader) != "sync"
        ):
            raise ValueError("usage_reader must be a synchronous callable")
        kind = _kind(function)
        if node and kind in {"generator", "async_generator"}:
            raise ValueError(
                "generator nodes are unsupported; compiled graph streams are supported"
            )
        settings = (self, boundary, options, usage_reader, node, branch_id, step_reader)
        marker = getattr(function, "__agentloop_langgraph_harness__", None)
        if (
            isinstance(marker, tuple)
            and len(marker) == 2
            and isinstance(marker[0], ReferenceType)
            and marker[0]() is function
        ):
            if marker[1] == settings:
                return function
            raise ValueError("wrap the original callable to change harness settings")

        def prepare(args, kwargs):
            session = self._session.get()
            if session is None:
                raise RuntimeError("active LangGraph controls require adapter.runnable(app)")
            branch, dispatch = self._branch.get(), options
            if node:
                task_id, attempt = _runtime_identity()
                branch = branch_id or task_id
                step = step_reader(*args, **kwargs) if step_reader is not None else options.step
                if type(step) is not StepInfo:
                    raise ValueError("step_reader must return StepInfo")
                dispatch = replace(
                    options, step=step, retry_source="framework" if attempt > 1 else None
                )
            protected = session.run.wrap(
                function,
                boundary=boundary,
                branch_id=branch,
                dispatch=dispatch,
                usage_reader=usage_reader,
            )
            return protected, lambda: self._scope(session, branch)

        if kind == "async":

            @wraps(function)
            async def wrapped(*args, **kwargs):
                protected, scope = prepare(args, kwargs)
                with scope():
                    return await protected(*args, **kwargs)
        elif kind == "generator":

            @wraps(function)
            def wrapped(*args, **kwargs):
                protected, scope = prepare(args, kwargs)
                with scope():
                    source = protected(*args, **kwargs)
                return (yield from _ScopedIterator(source, scope))
        elif kind == "async_generator":

            @wraps(function)
            async def wrapped(*args, **kwargs):
                protected, scope = prepare(args, kwargs)
                source = _ScopedAsyncIterator(protected(*args, **kwargs), scope)
                # Forward sends, throws and close instead of reducing to async-for.
                try:
                    item = await source.__anext__()
                    while True:
                        try:
                            sent = yield item
                        except GeneratorExit:
                            await source.aclose()
                            raise
                        except BaseException as exc:
                            item = await source.athrow(exc)
                        else:
                            item = await source.asend(sent)
                except StopAsyncIteration:
                    return
        else:

            @wraps(function)
            def wrapped(*args, **kwargs):
                protected, scope = prepare(args, kwargs)
                with scope():
                    return protected(*args, **kwargs)

        wrapped.__agentloop_langgraph_harness__ = (ref(wrapped), settings)
        return wrapped

    def runnable(self, app):
        """Wrap invoke/ainvoke/stream/astream; disabled mode returns app itself."""
        if self._harness.config.mode == "disabled":
            return app
        if isinstance(app, ControlledRunnable):
            if app._adapter is self:
                return app
            raise ValueError("runnable is already controlled by another adapter")
        _check_version()
        for method in ("invoke", "ainvoke", "stream", "astream"):
            if not callable(getattr(app, method, None)):
                raise TypeError(f"compiled app must provide {method}")
        return ControlledRunnable(self, app)

    def _complete(self, session, value):
        if Hook("completion") not in self.capabilities.hooks:
            return value
        return session.run.wrap(_identity, boundary="completion")(value)


class ControlledRunnable:
    """The four controlled entrypoints; access the original app for other APIs.

    This intentionally does not proxy batch or configuration methods that could
    return an unprotected runnable. Rewrap such a runnable explicitly.
    """

    def __init__(self, adapter, app):
        self._adapter, self.app = adapter, app

    def invoke(self, *args, **kwargs):
        adapter = self._adapter
        session = adapter._start()
        with adapter._scope(session, "main", root=True):
            return adapter._complete(session, self.app.invoke(*args, **kwargs))

    async def ainvoke(self, *args, **kwargs):
        adapter = self._adapter
        session = adapter._start()
        with adapter._scope(session, "main", root=True):
            return adapter._complete(session, await self.app.ainvoke(*args, **kwargs))

    def stream(self, *args, **kwargs):
        adapter = self._adapter
        session = adapter._start()

        def scope():
            return adapter._scope(session, "main", root=True)

        with scope():
            source = self.app.stream(*args, **kwargs)
        result = yield from _ScopedIterator(source, scope)
        with scope():
            return adapter._complete(session, result)

    async def astream(self, *args, **kwargs):
        adapter = self._adapter
        session = adapter._start()

        def scope():
            return adapter._scope(session, "main", root=True)

        with scope():
            source = self.app.astream(*args, **kwargs)
        source = _ScopedAsyncIterator(source, scope)
        try:
            item = await source.__anext__()
            while True:
                try:
                    sent = yield item
                except GeneratorExit:
                    await source.aclose()
                    raise
                except BaseException as exc:
                    item = await source.athrow(exc)
                else:
                    item = await source.asend(sent)
        except StopAsyncIteration:
            with scope():
                adapter._complete(session, None)
