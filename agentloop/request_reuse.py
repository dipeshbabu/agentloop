"""Request-local reuse of declared-safe results, and protected context deduplication (#399).

Reuse is scoped to one request. Open a scope where your application handles a
request, and decorate the tool reads that are safe to repeat:

```python
from agentloop.request_reuse import request_scope, reusable


@reusable(tool="catalog.lookup", version="2", effect="read")
def lookup(sku): ...


with request_scope("req-42", auth_scope="tenant:acme/user:7"):
    lookup("A1")  # runs
    lookup("A1")  # reused within this request
```

Rules, all enforced:

* Only tools declared ``effect="read"`` or ``"none"`` can be decorated. Model
  output reuse needs ``kind="model"`` and ``allow_model_output=True``.
* The cache exists only inside a ``request_scope``. Outside one, calls run
  normally and nothing is stored, so results never cross requests or tenants.
* A key covers the tool, its version, the scope's ``auth_scope``, the optional
  ``data_version`` and the canonical JSON of the call's arguments. A hit
  compares the stored canonical input in full, not just its hash.
* Arguments that aren't plain JSON values, and results that can't be deep
  copied, bypass the cache. Errors are never cached.
* Results are deep copied on store and on every hit, so a caller that mutates
  one cannot change what another sees.
* ``invalidate()`` drops entries, for example after a write in the same request.

When a trace is active, each call records a tool span with
``agentloop.reuse`` metadata (``hit``, ``reason``, ``key_sha256``) plus the
existing ``read_only`` and ``tool_reuse_key`` declarations.
"""

from __future__ import annotations

import contextvars
import copy
import functools
import hashlib
import inspect
import json
import math
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, TypeVar

from agentloop.tracer import current_trace, trace_tool_call

F = TypeVar("F", bound=Callable[..., Any])
REUSE_KEY = "agentloop.reuse"
SAFE_EFFECTS = frozenset({"none", "read"})
_MAX_KEY_INPUT = 1_000_000


class ReuseError(ValueError):
    """A reuse declaration is unsafe or malformed."""


class _Scope:
    def __init__(self, request_id: str, auth_scope: str) -> None:
        self.request_id = request_id
        self.auth_scope = auth_scope
        self._entries: dict[str, tuple[str, Any]] = {}
        self._lock = threading.Lock()
        self.stats = {"hits": 0, "misses": 0, "bypassed": 0, "invalidated": 0}

    def get(self, key: str, canonical_input: str) -> tuple[bool, Any]:
        with self._lock:
            entry = self._entries.get(key)
        if entry is None or entry[0] != canonical_input:
            return False, None
        return True, entry[1]

    def put(self, key: str, canonical_input: str, value: Any) -> None:
        with self._lock:
            # The first completed result wins if two calls raced.
            self._entries.setdefault(key, (canonical_input, value))

    def invalidate(self, tool: str | None) -> int:
        with self._lock:
            if tool is None:
                removed = len(self._entries)
                self._entries.clear()
            else:
                prefix = f"{tool}\x00"
                doomed = [key for key in self._entries if key.startswith(prefix)]
                for key in doomed:
                    del self._entries[key]
                removed = len(doomed)
            self.stats["invalidated"] += removed
            return removed

    def count(self, outcome: str) -> None:
        with self._lock:
            self.stats[outcome] += 1


_scope: contextvars.ContextVar[_Scope | None] = contextvars.ContextVar(
    "agentloop_request_reuse_scope", default=None
)


def _label(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ReuseError(f"{name} must be nonempty text of at most 256 characters")
    return value


@contextmanager
def request_scope(request_id: str, *, auth_scope: str) -> Iterator[_Scope]:
    """Open a reuse scope for one request. Nested scopes are refused."""
    _label(request_id, "request_id")
    _label(auth_scope, "auth_scope")
    if _scope.get() is not None:
        raise ReuseError("a request scope is already open; reuse never spans requests")
    scope = _Scope(request_id, auth_scope)
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)


def invalidate(tool: str | None = None) -> int:
    """Drop cached results in the current request, for one tool or all. Returns the count."""
    scope = _scope.get()
    return 0 if scope is None else scope.invalidate(tool)


def _canonical_input(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> str | None:
    try:
        text = json.dumps(
            {"args": list(args), "kwargs": dict(kwargs)},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            ensure_ascii=False,
        )
    except (TypeError, ValueError, RecursionError):
        return None
    if len(text) > _MAX_KEY_INPUT:
        return None
    # Reject values JSON would silently coerce: tuples become lists, int keys become strings.
    if json.loads(text) != {"args": _plain(list(args)), "kwargs": _plain(dict(kwargs))}:
        return None
    return text


def _plain(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else object()
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _plain(item) for key, item in value.items()}
    return object()  # never equal: forces a bypass


def reusable(
    *,
    tool: str,
    version: str,
    effect: str,
    data_version: Callable[[], str] | None = None,
    kind: str = "tool",
    allow_model_output: bool = False,
) -> Callable[[F], F]:
    """Reuse a declared-safe call's result within one request. See the module docs."""
    _label(tool, "tool")
    _label(version, "version")
    if "\x00" in tool:
        raise ReuseError("tool must not contain NUL")
    if effect not in SAFE_EFFECTS:
        raise ReuseError(
            f"effect {effect!r} cannot be reused; only {sorted(SAFE_EFFECTS)} are safe to repeat"
        )
    if kind not in {"tool", "model"}:
        raise ReuseError("kind must be 'tool' or 'model'")
    if kind == "model" and allow_model_output is not True:
        raise ReuseError("reusing model output needs allow_model_output=True")
    if data_version is not None and not callable(data_version):
        raise ReuseError("data_version must be a callable returning the data's current version")

    def decorator(func: F) -> F:
        if (
            inspect.iscoroutinefunction(func)
            or inspect.isgeneratorfunction(func)
            or (inspect.isasyncgenfunction(func))
        ):
            raise ReuseError("only synchronous, non-generator functions can be reused")

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            scope = _scope.get()
            if scope is None:
                return func(*args, **kwargs)
            canonical_input = _canonical_input(args, kwargs)
            version_value = data_version() if data_version is not None else None
            if version_value is not None and not isinstance(version_value, str):
                raise ReuseError("data_version() must return text")
            if canonical_input is None:
                scope.count("bypassed")
                return _traced(func, args, kwargs, tool, effect, None, "uncanonical_input")
            material = json.dumps(
                [tool, version, scope.auth_scope, version_value, canonical_input],
                separators=(",", ":"),
                ensure_ascii=False,
            )
            digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
            key = f"{tool}\x00{digest}"
            hit, value = scope.get(key, material)
            if hit:
                scope.count("hits")
                return _traced(
                    lambda: copy.deepcopy(value), (), {}, tool, effect, digest, "hit", hit=True
                )
            result = _traced(func, args, kwargs, tool, effect, digest, "miss")
            try:
                stored = copy.deepcopy(result)
            except Exception:  # noqa: BLE001 - any copy failure means "don't cache"
                scope.count("bypassed")
                return result
            scope.put(key, material, stored)
            scope.count("misses")
            return result

        wrapper.__agentloop_reuse__ = {  # type: ignore[attr-defined]
            "tool": tool,
            "version": version,
            "effect": effect,
            "kind": kind,
        }
        return wrapper  # type: ignore[return-value]

    return decorator


def _traced(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
    tool: str,
    effect: str,
    digest: str | None,
    reason: str,
    *,
    hit: bool = False,
) -> Any:
    if current_trace() is None:
        return func(*args, **kwargs)
    metadata: dict[str, Any] = {
        REUSE_KEY: {"hit": hit, "reason": reason, "key_sha256": digest},
        "read_only": True,
        "agentloop.effects": {"effect": effect},
    }
    if digest is not None:
        metadata["tool_reuse_key"] = digest[:64]
    with trace_tool_call(tool, metadata=metadata):
        return func(*args, **kwargs)


# Context deduplication ------------------------------------------------------------


def deduplicate_context(segments: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Drop later exact duplicates of context segments, never a protected one.

    Each segment is ``{"id": str, "text": str, "protected": bool}``; other keys
    are kept as they are. A segment is removed only when it is unprotected and
    its text exactly equals an earlier kept segment's text. Protected segments,
    such as system instructions, always stay, even when they repeat.
    """
    seen: dict[str, str] = {}
    kept: list[dict[str, Any]] = []
    removed: list[dict[str, str]] = []
    ids: set[str] = set()
    for segment in segments:
        if not isinstance(segment, Mapping):
            raise ReuseError("each segment must be a mapping")
        identity, text = segment.get("id"), segment.get("text")
        if not isinstance(identity, str) or not identity or identity in ids:
            raise ReuseError("each segment needs a unique, nonempty id")
        if not isinstance(text, str):
            raise ReuseError(f"segment {identity!r} needs text")
        protected = segment.get("protected", False)
        if not isinstance(protected, bool):
            raise ReuseError(f"segment {identity!r} has a non-boolean protected flag")
        ids.add(identity)
        if not protected and text in seen:
            removed.append({"id": identity, "duplicate_of": seen[text]})
            continue
        seen.setdefault(text, identity)
        kept.append(dict(segment))
    before = sum(len(str(s.get("text", ""))) for s in segments)
    after = sum(len(s["text"]) for s in kept)
    return {
        "segments": kept,
        "removed": removed,
        "characters_before": before,
        "characters_after": after,
        "protected_kept": sum(1 for s in kept if s.get("protected") is True),
    }


# Baseline versus candidate ---------------------------------------------------------


def compare_reuse(
    baseline: Any,
    candidate: Any,
    *,
    baseline_correct: bool | None,
    candidate_correct: bool | None,
) -> dict[str, Any]:
    """Compare two recorded runs: calls, tokens, latency, cost and correctness.

    Correctness comes from your own checks; ``None`` means unknown, and an
    unknown or failed candidate is never reported as an improvement. Cost is
    summed only when every model call in a run has a known cost; otherwise it
    is ``None`` and stays unknown.
    """
    for name, value in (
        ("baseline_correct", baseline_correct),
        ("candidate_correct", candidate_correct),
    ):
        if value is not None and not isinstance(value, bool):
            raise ReuseError(f"{name} must be True, False or None")
    base, cand = _run_summary(baseline), _run_summary(candidate)
    delta = {
        key: None if base[key] is None or cand[key] is None else round(base[key] - cand[key], 3)
        for key in ("tool_calls", "executed_tool_calls", "tokens", "runtime_ms", "cost_usd")
    }
    if candidate_correct is True and baseline_correct is not False:
        verdict = (
            "improved"
            if (delta["runtime_ms"] or 0) > 0 or (delta["tokens"] or 0) > 0
            else ("no_gain")
        )
    elif candidate_correct is False:
        verdict = "rejected_incorrect"
    else:
        verdict = "unverified"
    return {
        "baseline": {**base, "correct": baseline_correct},
        "candidate": {**cand, "correct": candidate_correct},
        "savings": delta,
        "verdict": verdict,
        "evidence": "measured",
    }


def _run_summary(trace: Any) -> dict[str, Any]:
    events = list(getattr(trace, "events", []) or [])
    tools = [e for e in events if getattr(e, "event_type", None) == "tool_call"]
    hits = [e for e in tools if ((e.metadata or {}).get(REUSE_KEY) or {}).get("hit") is True]
    models = [e for e in events if getattr(e, "event_type", None) == "model_call"]
    costs: list[Any] = [(e.metadata or {}).get("cost_usd") for e in models]
    known_cost = all(isinstance(c, int | float) and not isinstance(c, bool) for c in costs)
    runtime = getattr(trace, "elapsed_ms", None)
    return {
        "tool_calls": len(tools),
        "executed_tool_calls": len(tools) - len(hits),
        "reused_tool_calls": len(hits),
        "tokens": sum((e.input_tokens or 0) + (e.output_tokens or 0) for e in models),
        "runtime_ms": round(float(runtime), 3) if isinstance(runtime, int | float) else None,
        "cost_usd": round(sum(costs), 6) if known_cost and costs else (0.0 if not models else None),
    }


def scope_stats() -> dict[str, int] | None:
    """Hit, miss, bypass and invalidation counts for the current request, if any."""
    scope = _scope.get()
    return None if scope is None else dict(scope.stats)


__all__ = [
    "REUSE_KEY",
    "ReuseError",
    "compare_reuse",
    "deduplicate_context",
    "invalidate",
    "request_scope",
    "reusable",
    "scope_stats",
]
