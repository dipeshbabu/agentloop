from __future__ import annotations

import inspect
import time
from functools import wraps
from typing import Any

from agentloop.events import utc_now_iso
from agentloop.tracer import current_event_id, current_trace, record_model_call

# Marker set on wrappers so repeated instrumentation of the same callable is a
# no-op instead of stacking wrappers and double-recording.
_INSTRUMENTED_FLAG = "__agentloop_instrumented__"


def _usage_get(usage: Any, *names: str) -> int:
    for name in names:
        if usage is None:
            continue
        if isinstance(usage, dict) and name in usage:
            return int(usage.get(name) or 0)
        value = getattr(usage, name, None)
        if value is not None:
            return int(value or 0)
    return 0


def _find_usage(obj: Any) -> Any:
    """Locate a usage object on a response or a streaming event.

    Non-streaming responses and chat chunks expose ``usage`` directly. The locked
    Responses streaming API instead delivers final usage on the terminal
    ``ResponseCompletedEvent`` under ``event.response.usage`` (the event itself has
    no ``usage``), so fall back to the nested response shape.
    """

    if obj is None:
        return None
    usage = getattr(obj, "usage", None)
    if usage is None and isinstance(obj, dict):
        usage = obj.get("usage")
    if usage is not None:
        return usage
    response = getattr(obj, "response", None)
    if response is None and isinstance(obj, dict):
        response = obj.get("response")
    if response is None:
        return None
    nested = getattr(response, "usage", None)
    if nested is None and isinstance(response, dict):
        nested = response.get("usage")
    return nested


def _extract_usage(result: Any) -> tuple[int, int]:
    usage = _find_usage(result)
    input_tokens = _usage_get(usage, "input_tokens", "prompt_tokens")
    output_tokens = _usage_get(usage, "output_tokens", "completion_tokens")
    return input_tokens, output_tokens


def _metadata_from_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {"integration": "openai"}
    if "tools" in kwargs and kwargs["tools"] is not None:
        try:
            metadata["tool_count"] = len(kwargs["tools"])
        except TypeError:
            metadata["tool_count"] = 1
    if "messages" in kwargs and kwargs["messages"] is not None:
        try:
            metadata["message_count"] = len(kwargs["messages"])
        except TypeError:
            metadata["message_count"] = 1
    if args:
        metadata["positional_args"] = len(args)
    return metadata


def _model_from_call(kwargs: dict[str, Any], default_model: str | None = None) -> str | None:
    return (
        str(kwargs.get("model") or default_model)
        if (kwargs.get("model") or default_model)
        else None
    )


class _CallRecorder:
    """Collects timing for one instrumented call and records it exactly once.

    Recording is deferred so streaming calls can be finalized when the stream is
    consumed rather than when the iterator is created. Trace ownership is captured
    at invocation time: the event is recorded into the trace that was active when
    the call was made, regardless of which context consumes the stream later. If no
    trace was active at invocation the call is never recorded, and the application
    call is not disturbed.
    """

    def __init__(
        self,
        name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        default_model: str | None,
    ) -> None:
        self._name = name
        self._args = args
        self._kwargs = kwargs
        self._default_model = default_model
        self._started_at = utc_now_iso()
        self._start = time.perf_counter()
        self._done = False
        # Capture ownership now, at invocation time, not at finalization time.
        self._trace = current_trace()
        self._parent_id = current_event_id()

    def finalize(self, result: Any = None, *, status: str = "ok", error: str | None = None) -> None:
        if self._done:
            return
        self._done = True
        if self._trace is None:
            # No trace was active when the call was invoked: never record, and do
            # not turn the caller's successful application call into an error.
            return
        input_tokens, output_tokens = _extract_usage(result)
        record_model_call(
            self._name,
            started_at=self._started_at,
            duration_ms=(time.perf_counter() - self._start) * 1000,
            model=_model_from_call(self._kwargs, self._default_model),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            status=status,
            error=error,
            metadata=_metadata_from_call(self._args, self._kwargs),
            parent_id=self._parent_id,
            trace=self._trace,
        )


def _has_usage(obj: Any) -> bool:
    return _find_usage(obj) is not None


def _stream_requested(kwargs: dict[str, Any]) -> bool:
    return bool(kwargs.get("stream"))


def _is_sync_stream(result: Any) -> bool:
    if isinstance(result, (dict, list, tuple, set, str, bytes, bytearray)):
        return False
    return (
        hasattr(result, "__iter__") or hasattr(result, "__next__") or hasattr(result, "__enter__")
    )


def _is_async_stream(result: Any) -> bool:
    return (
        hasattr(result, "__aiter__")
        or hasattr(result, "__anext__")
        or hasattr(result, "__aenter__")
        or inspect.isasyncgen(result)
    )


class _InstrumentedSyncStream:
    """Proxy over a sync streaming response that finalizes on consumption.

    Iteration, context-manager use, and ``close()`` all end the stream; whichever
    happens first records the call (once). Every other attribute is delegated to
    the wrapped stream so SDK behavior is preserved.
    """

    def __init__(self, stream: Any, recorder: _CallRecorder) -> None:
        self._stream = stream
        self._recorder = recorder
        self._iter: Any = None
        self._usage_source: Any = None

    def __iter__(self) -> _InstrumentedSyncStream:
        self._iter = iter(self._stream)
        return self

    def __next__(self) -> Any:
        if self._iter is None:
            self._iter = iter(self._stream)
        try:
            chunk = next(self._iter)
        except StopIteration:
            self._recorder.finalize(self._usage_source)
            raise
        except BaseException as exc:  # noqa: BLE001 - record then propagate unchanged
            self._recorder.finalize(self._usage_source, status="error", error=str(exc))
            raise
        if _has_usage(chunk):
            self._usage_source = chunk
        return chunk

    def __enter__(self) -> _InstrumentedSyncStream:
        if hasattr(self._stream, "__enter__"):
            self._stream.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        suppress = None
        if hasattr(self._stream, "__exit__"):
            suppress = self._stream.__exit__(exc_type, exc, tb)
        if exc is not None:
            self._recorder.finalize(self._usage_source, status="error", error=str(exc))
        else:
            self._recorder.finalize(self._usage_source)
        return suppress

    def close(self) -> Any:
        try:
            if hasattr(self._stream, "close"):
                return self._stream.close()
            return None
        finally:
            self._recorder.finalize(self._usage_source)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._stream, item)


class _InstrumentedAsyncStream:
    """Async counterpart of :class:`_InstrumentedSyncStream`."""

    def __init__(self, stream: Any, recorder: _CallRecorder) -> None:
        self._stream = stream
        self._recorder = recorder
        self._iter: Any = None
        self._usage_source: Any = None

    def __aiter__(self) -> _InstrumentedAsyncStream:
        self._iter = (
            self._stream.__aiter__() if hasattr(self._stream, "__aiter__") else self._stream
        )
        return self

    async def __anext__(self) -> Any:
        if self._iter is None:
            self._iter = (
                self._stream.__aiter__() if hasattr(self._stream, "__aiter__") else self._stream
            )
        try:
            chunk = await self._iter.__anext__()
        except StopAsyncIteration:
            self._recorder.finalize(self._usage_source)
            raise
        except BaseException as exc:  # noqa: BLE001 - record then propagate unchanged
            self._recorder.finalize(self._usage_source, status="error", error=str(exc))
            raise
        if _has_usage(chunk):
            self._usage_source = chunk
        return chunk

    async def __aenter__(self) -> _InstrumentedAsyncStream:
        if hasattr(self._stream, "__aenter__"):
            await self._stream.__aenter__()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        suppress = None
        if hasattr(self._stream, "__aexit__"):
            suppress = await self._stream.__aexit__(exc_type, exc, tb)
        if exc is not None:
            self._recorder.finalize(self._usage_source, status="error", error=str(exc))
        else:
            self._recorder.finalize(self._usage_source)
        return suppress

    async def aclose(self) -> Any:
        try:
            if hasattr(self._stream, "aclose"):
                return await self._stream.aclose()
            return None
        finally:
            self._recorder.finalize(self._usage_source)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._stream, item)


def instrument_callable(
    fn: Any,
    *,
    name: str = "openai.model_call",
    default_model: str | None = None,
) -> Any:
    """Wrap a sync or async OpenAI-like callable and record token usage.

    The wrapper is dependency-free and works with OpenAI SDK resource methods such
    as `client.responses.create(...)` or `client.chat.completions.create(...)`.
    It records prompt/completion usage when the returned object exposes a
    `.usage` object or a `{"usage": ...}` dict.

    Wrapping is idempotent: re-instrumenting an already-instrumented callable
    returns it unchanged, so one request records one event. Streaming responses
    (requested with `stream=True`) are finalized when the stream is consumed,
    closed, fails, or is cancelled — not when the iterator is created — so the
    recorded duration covers consumption and final usage is captured when the SDK
    provides it. Calls made without an active trace are not recorded and are not
    turned into errors.
    """

    if getattr(fn, _INSTRUMENTED_FLAG, False):
        return fn

    if inspect.iscoroutinefunction(fn):

        @wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            recorder = _CallRecorder(name, args, kwargs, default_model)
            try:
                result = await fn(*args, **kwargs)
            except Exception as exc:
                recorder.finalize(None, status="error", error=str(exc))
                raise
            if _stream_requested(kwargs) and _is_async_stream(result):
                return _InstrumentedAsyncStream(result, recorder)
            recorder.finalize(result)
            return result

        setattr(async_wrapper, _INSTRUMENTED_FLAG, True)
        return async_wrapper

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        recorder = _CallRecorder(name, args, kwargs, default_model)
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            recorder.finalize(None, status="error", error=str(exc))
            raise
        if _stream_requested(kwargs) and _is_sync_stream(result):
            return _InstrumentedSyncStream(result, recorder)
        recorder.finalize(result)
        return result

    setattr(wrapper, _INSTRUMENTED_FLAG, True)
    return wrapper


def instrument_openai_client(client: Any) -> Any:
    """Patch common OpenAI SDK call sites in-place.

    Supported call sites when present:
    - client.responses.create
    - client.chat.completions.create

    The function returns the same client object for ergonomic usage:

    ```python
    client = instrument_openai_client(OpenAI())
    ```
    """

    responses = getattr(client, "responses", None)
    if responses is not None and callable(getattr(responses, "create", None)):
        responses.create = instrument_callable(responses.create, name="openai.responses.create")

    chat = getattr(client, "chat", None)
    completions = getattr(chat, "completions", None) if chat is not None else None
    if completions is not None and callable(getattr(completions, "create", None)):
        completions.create = instrument_callable(
            completions.create, name="openai.chat.completions.create"
        )

    return client
