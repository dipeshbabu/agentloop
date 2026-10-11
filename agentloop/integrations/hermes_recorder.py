"""Standalone Hermes observer exporting native AgentLoop 1.1 traces (Apache-2.0).

This module deliberately uses only the standard library so its directory-plugin
copy runs inside Hermes without changing Hermes's managed Python dependencies.
Hook contract: NousResearch/hermes-agent v0.21.6, revision 818c13be1dc4.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
import uuid
from collections import OrderedDict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOOKS = (
    "pre_llm_call",
    "pre_api_request",
    "post_api_request",
    "api_request_error",
    "post_tool_call",
    "post_llm_call",
    "on_session_end",
    "on_session_finalize",
    "subagent_start",
    "subagent_stop",
)
_LOGGER = logging.getLogger("agentloop.hermes")
_SCHEMA_VERSION = "1.1"


def _identifier(value):
    return value if isinstance(value, str) and 0 < len(value) <= 512 else None


def _label(value, default="unknown"):
    return value[:256] if isinstance(value, str) and value else default


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def _timestamp(value):
    number = _number(value)
    if number is None:
        return None
    try:
        return datetime.fromtimestamp(number, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _count(value):
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def _usage(value):
    """Hermes canonical input buckets are disjoint; reasoning is within output."""
    if not isinstance(value, dict):
        return 0, 0, "unavailable", {}
    buckets = {
        key: _count(value.get(key))
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
        )
    }
    output = buckets["output_tokens"]
    prompt = _count(value.get("prompt_tokens"))
    if prompt is None and all(
        buckets[key] is not None
        for key in ("input_tokens", "cache_read_tokens", "cache_write_tokens")
    ):
        prompt = sum(
            buckets[key] for key in ("input_tokens", "cache_read_tokens", "cache_write_tokens")
        )
    if prompt is None or output is None:
        return 0, 0, "unavailable", {key: val for key, val in buckets.items() if val is not None}
    return prompt, output, "provider", {key: val for key, val in buckets.items() if val is not None}


class HermesRecorder:
    """One bounded, thread-safe recorder per enabled Hermes plugin/profile.

    All hooks return None. Output text is opt-in. Raw prompts, arguments, errors,
    requests, and histories are never copied, including with capture enabled.
    """

    def __init__(self, export_dir, *, capture_content=False, max_active_turns=64, max_events=2048):
        if type(capture_content) is not bool:
            raise ValueError("capture_content must be boolean")
        if type(max_active_turns) is not int or not 1 <= max_active_turns <= 1024:
            raise ValueError("max_active_turns must be between 1 and 1024")
        if type(max_events) is not int or not 1 <= max_events <= 10000:
            raise ValueError("max_events must be between 1 and 10000")
        self.export_dir = Path(export_dir).expanduser()
        self.capture_content = capture_content
        self.max_active_turns = max_active_turns
        self.max_events = max_events
        self._lock = threading.RLock()
        self._turns = OrderedDict()
        self._closed = OrderedDict()
        self._exports = deque(maxlen=100)
        self._retry_exports = OrderedDict()
        self._dropped_exports = 0
        self._errors = 0
        self._unscoped = 0

    @property
    def exported_paths(self):
        with self._lock:
            return tuple(self._exports)

    def summary(self):
        with self._lock:
            return {
                "active_turns": len(self._turns),
                "export_errors": self._errors,
                "unscoped_events": self._unscoped,
                "recent_exports": len(self._exports),
                "pending_exports": len(self._retry_exports),
                "dropped_exports": self._dropped_exports,
            }

    def register(self, ctx):
        for hook in HOOKS:
            ctx.register_hook(hook, getattr(self, hook))

    def _key(self, data):
        session = _identifier(data.get("session_id"))
        task = _identifier(data.get("task_id"))
        turn = _identifier(data.get("turn_id"))
        if turn is None and task is None:
            return None
        return session, task, turn

    def _turn(self, data, *, missing_start=True):
        key = self._key(data)
        if key is None:
            self._unscoped += 1
            return None
        if key in self._closed:
            return None
        state = self._turns.get(key)
        if state is not None:
            return state
        if len(self._turns) >= self.max_active_turns:
            # Do not discard evidence or join unrelated turns when at capacity.
            self._unscoped += 1
            return None
        now = datetime.now(timezone.utc)
        run_id = "run_hermes_" + uuid.uuid4().hex
        info = {
            "session_id": key[0],
            "task_id": key[1],
            "turn_id": key[2],
            "capture_content": self.capture_content,
            "capture_complete": not missing_start,
            "missing_turn_start": missing_start,
            "events_dropped": 0,
            "timing": "Host request timestamps; inferred tool intervals; observed turn boundary.",
            "coverage": "Main-loop provider attempts and tool callbacks; auxiliary model traffic is not captured.",
        }
        state = {
            "trace": {
                "schema_version": _SCHEMA_VERSION,
                "name": "hermes.turn",
                "run_id": run_id,
                "started_at": now.isoformat(),
                "ended_at": None,
                "elapsed_ms": None,
                "metadata": {"integration": "hermes", "hermes": info},
                "events": [],
            },
            "root_id": "evt_" + uuid.uuid4().hex,
            "start_perf": time.perf_counter(),
            "seen": set(),
            "pending": {},
            "key": key,
        }
        self._turns[key] = state
        return state

    def _append(self, state, identity, event):
        if identity in state["seen"]:
            return
        if len(state["seen"]) >= self.max_events:
            info = state["trace"]["metadata"]["hermes"]
            info["events_dropped"] += 1
            info["capture_complete"] = False
            return
        state["seen"].add(identity)
        state["trace"]["events"].append(event)

    def _event(self, state, name, kind, start, end, duration, *, status="ok", metadata=None):
        return {
            "event_id": "evt_" + uuid.uuid4().hex,
            "run_id": state["trace"]["run_id"],
            "event_type": kind,
            "name": _label(name),
            "started_at": start.isoformat(),
            "ended_at": end.isoformat(),
            "duration_ms": duration,
            "parent_id": state["root_id"],
            "status": status,
            "metadata": {"integration": "hermes", **(metadata or {})},
        }

    def pre_llm_call(self, **data):
        with self._lock:
            self._turn(data, missing_start=False)

    def pre_api_request(self, **data):
        with self._lock:
            state = self._turn(data)
            if state is None:
                return
            request = _identifier(data.get("api_request_id"))
            if request is None:
                state["trace"]["metadata"]["hermes"]["capture_complete"] = False
                return
            retry = _count(data.get("retry_count")) or 0
            identity = request, retry
            if ("request", request, retry) in state["seen"]:
                return
            if identity not in state["pending"] and len(state["pending"]) < self.max_events:
                state["pending"][identity] = _timestamp(data.get("started_at")) or datetime.now(
                    timezone.utc
                )
            if retry:
                start = state["pending"].get(identity, datetime.now(timezone.utc))
                event = self._event(
                    state,
                    "hermes.provider_retry",
                    "retry",
                    start,
                    start,
                    0,
                    metadata={"retry_count": retry, "api_request_id": request},
                )
                self._append(state, ("retry", request, retry), event)

    def _request_done(self, data, *, failed):
        with self._lock:
            state = self._turn(data)
            if state is None:
                return
            request = _identifier(data.get("api_request_id"))
            if request is None:
                state["trace"]["metadata"]["hermes"]["capture_complete"] = False
                return
            retry = _count(data.get("retry_count"))
            # Success payloads omit retry_count; match the most recent pending attempt.
            if retry is None:
                attempts = [index for (rid, index) in state["pending"] if rid == request]
                retry = max(attempts, default=0)
            identity = request, retry
            if ("request", request, retry) in state["seen"]:
                return
            pending = state["pending"].pop(identity, None)
            start, end = _timestamp(data.get("started_at")), _timestamp(data.get("ended_at"))
            duration_s = _number(data.get("api_duration"))
            if duration_s is not None and duration_s > 86400:
                duration_s = None
                state["trace"]["metadata"]["hermes"]["capture_complete"] = False
            timing = "host_timestamps"
            if start is None or end is None or end < start:
                end = datetime.now(timezone.utc)
                start = min(pending, end) if pending is not None else end
                timing = "observer_fallback"
                state["trace"]["metadata"]["hermes"]["capture_complete"] = False
            duration = (
                duration_s * 1000
                if duration_s is not None
                else max(0, (end - start).total_seconds() * 1000)
            )
            metadata = {
                "api_request_id": request,
                "retry_count": retry,
                "timing_source": timing,
                "provider": _label(data.get("provider")),
                "usage_source": "hermes_normalized_provider_usage",
            }
            if failed:
                metadata["retryable"] = data.get("retryable") is True
                status_code = _count(data.get("status_code"))
                if status_code is not None:
                    metadata["status_code"] = status_code
            event = self._event(
                state,
                "hermes.model_request",
                "model_call",
                start,
                end,
                duration,
                status="error" if failed else "ok",
                metadata=metadata,
            )
            event["model"] = _label(data.get("response_model") or data.get("model"))
            inp, out, basis, buckets = _usage(None if failed else data.get("usage"))
            event.update(input_tokens=inp, output_tokens=out, token_provenance=basis)
            event["metadata"]["usage_buckets"] = buckets
            if self.capture_content:
                message = data.get("assistant_message")
                text = (
                    message.get("content")
                    if isinstance(message, dict)
                    else getattr(message, "content", None)
                )
                if isinstance(text, str) and len(text) <= 10000:
                    event["output_text"] = text
                elif isinstance(text, str):
                    event["metadata"]["output_omitted_too_large"] = True
            self._append(state, ("request", request, retry), event)

    def post_api_request(self, **data):
        self._request_done(data, failed=False)

    def api_request_error(self, **data):
        self._request_done(data, failed=True)

    def post_tool_call(self, **data):
        with self._lock:
            state = self._turn(data)
            if state is None:
                return
            tool_id = _identifier(data.get("tool_call_id"))
            end = datetime.now(timezone.utc)
            duration = _number(data.get("duration_ms"))
            if duration is None or duration > 86_400_000:
                duration = 0
                state["trace"]["metadata"]["hermes"]["capture_complete"] = False
            start = end - timedelta(milliseconds=duration)
            status = _label(data.get("status"))
            if status not in {"ok", "success", "error", "blocked"}:
                state["trace"]["metadata"]["hermes"]["capture_complete"] = False
            event = self._event(
                state,
                data.get("tool_name"),
                "tool_call",
                start,
                end,
                duration,
                status="ok" if status in {"ok", "success"} else "error",
                metadata={
                    "tool_call_id": tool_id,
                    "hermes_status": _label(status),
                    "timing_source": "host_duration_inferred_interval",
                },
            )
            if self.capture_content:
                result = data.get("result")
                if isinstance(result, str) and len(result) <= 10000:
                    event["output_text"] = result
                elif isinstance(result, str):
                    event["metadata"]["output_omitted_too_large"] = True
            self._append(state, ("tool", tool_id) if tool_id else ("tool", uuid.uuid4().hex), event)

    def post_llm_call(self, **data):
        with self._lock:
            state = self._turn(data)
            if state is not None and self.capture_content:
                result = data.get("assistant_response")
                if isinstance(result, str) and len(result) <= 10000:
                    state["trace"]["metadata"]["output"] = result
                elif isinstance(result, str):
                    state["trace"]["metadata"]["hermes"]["output_omitted_too_large"] = True
                    state["trace"]["metadata"]["output_unavailable"] = True

    def _close(self, key, outcome):
        state = self._turns.pop(key, None)
        if state is None:
            return None
        self._closed[key] = None
        while len(self._closed) > 4096:
            self._closed.popitem(last=False)
        trace = state["trace"]
        info = trace["metadata"]["hermes"]
        info["turn_outcome"] = outcome
        info["unfinished_requests"] = len(state["pending"])
        if state["pending"] or outcome == "incomplete":
            info["capture_complete"] = False
        now = datetime.now(timezone.utc)
        trace["ended_at"] = now.isoformat()
        trace["elapsed_ms"] = max(0, (time.perf_counter() - state["start_perf"]) * 1000)
        root = self._event(
            state,
            "hermes.turn",
            "tool_call",
            datetime.fromisoformat(trace["started_at"]),
            now,
            trace["elapsed_ms"],
            status="ok" if outcome == "completed" else "error",
            metadata={"operation_kind": "agent", "turn_outcome": outcome},
        )
        root.update(event_id=state["root_id"], parent_id=None)
        trace["events"].insert(0, root)
        return trace

    def _export(self, trace):
        if trace is None:
            return
        temporary = self.export_dir / ("." + trace["run_id"] + ".tmp")
        target = self.export_dir / (trace["run_id"] + ".json")
        created = False
        try:
            self.export_dir.mkdir(parents=True, exist_ok=True)
            with temporary.open("x", encoding="utf-8") as handle:
                created = True
                json.dump(trace, handle, indent=2, allow_nan=False)
            # Atomic publication without replacing an existing/competing artifact.
            os.link(temporary, target)
            with self._lock:
                self._exports.append(target)
                self._retry_exports.pop(trace["run_id"], None)
        except (OSError, ValueError, TypeError):
            with self._lock:
                self._errors += 1
                self._retry_exports[trace["run_id"]] = trace
                if len(self._retry_exports) > 16:
                    self._retry_exports.popitem(last=False)
                    self._dropped_exports += 1
            _LOGGER.warning(
                "AgentLoop could not export a Hermes trace; check the configured output directory."
            )
        finally:
            if created:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    _LOGGER.warning("AgentLoop could not remove an unfinished Hermes trace file.")

    def on_session_end(self, **data):
        with self._lock:
            key = self._key(data)
            if key is None:
                return  # Reduced legacy exit events cannot identify a particular turn.
            outcome = (
                "interrupted"
                if data.get("interrupted") is True
                else "failed"
                if data.get("failed") is True
                else "completed"
                if data.get("completed") is True
                else "incomplete"
            )
            trace = self._close(key, outcome)
        self._export(trace)

    def on_session_finalize(self, **data):
        session = _identifier(data.get("session_id"))
        if session is None:
            return
        with self._lock:
            traces = [
                self._close(key, "incomplete") for key in list(self._turns) if key[0] == session
            ]
        for trace in traces:
            self._export(trace)

    def _child(self, data, *, finished):
        session = _identifier(data.get("parent_session_id"))
        turn = _identifier(data.get("parent_turn_id"))
        child = _identifier(data.get("child_subagent_id"))
        if turn is None or child is None:
            return
        with self._lock:
            matches = [
                state for key, state in self._turns.items() if key[0] == session and key[2] == turn
            ]
            if len(matches) != 1:
                return
            children = matches[0]["trace"]["metadata"]["hermes"].setdefault("children", {})
            if child not in children and len(children) >= 64:
                matches[0]["trace"]["metadata"]["hermes"]["capture_complete"] = False
                return
            item = children.setdefault(
                child, {"child_session_id": _identifier(data.get("child_session_id"))}
            )
            if finished:
                item["status"] = _label(data.get("child_status"))

    def subagent_start(self, **data):
        self._child(data, finished=False)

    def subagent_stop(self, **data):
        self._child(data, finished=True)

    def flush(self):
        """Close unfinished turns and retry up to sixteen failed local exports."""
        with self._lock:
            traces = list(self._retry_exports.values())
            traces.extend(self._close(key, "incomplete") for key in list(self._turns))
        for trace in traces:
            self._export(trace)


def register(ctx):
    """Hermes plugin entry point; configuration and storage follow the active profile."""
    if getattr(ctx, "_agentloop_hermes_recorder", None) is not None:
        return
    from hermes_constants import get_hermes_home

    home = Path(get_hermes_home())
    directory = Path(ctx.get_config("export_dir", "agentloop/traces")).expanduser()
    if not directory.is_absolute():
        directory = home / directory
    recorder = HermesRecorder(
        directory,
        capture_content=ctx.get_config("capture_content", False),
        max_active_turns=ctx.get_config("max_active_turns", 64),
        max_events=ctx.get_config("max_events", 2048),
    )
    recorder.register(ctx)
    ctx._agentloop_hermes_recorder = recorder
    on_unload = getattr(ctx, "on_unload", None)
    if callable(on_unload):
        on_unload(recorder.flush)
