from __future__ import annotations

import inspect
import time
from functools import wraps
from typing import Any

from agentloop.events import utc_now_iso
from agentloop.tracer import record_model_call


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


def _extract_usage(result: Any) -> tuple[int, int]:
    usage = getattr(result, "usage", None)
    if usage is None and isinstance(result, dict):
        usage = result.get("usage")
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
    """

    if inspect.iscoroutinefunction(fn):

        @wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            started_at = utc_now_iso()
            start = time.perf_counter()
            status = "ok"
            error = None
            result = None
            try:
                result = await fn(*args, **kwargs)
                return result
            except Exception as exc:
                status = "error"
                error = str(exc)
                raise
            finally:
                input_tokens, output_tokens = _extract_usage(result)
                record_model_call(
                    name,
                    started_at=started_at,
                    duration_ms=(time.perf_counter() - start) * 1000,
                    model=_model_from_call(kwargs, default_model),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    status=status,
                    error=error,
                    metadata=_metadata_from_call(args, kwargs),
                )

        return async_wrapper

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        started_at = utc_now_iso()
        start = time.perf_counter()
        status = "ok"
        error = None
        result = None
        try:
            result = fn(*args, **kwargs)
            return result
        except Exception as exc:
            status = "error"
            error = str(exc)
            raise
        finally:
            input_tokens, output_tokens = _extract_usage(result)
            record_model_call(
                name,
                started_at=started_at,
                duration_ms=(time.perf_counter() - start) * 1000,
                model=_model_from_call(kwargs, default_model),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                status=status,
                error=error,
                metadata=_metadata_from_call(args, kwargs),
            )

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
