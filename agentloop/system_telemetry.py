"""Optional process, subprocess and inference telemetry for agent spans (issue #405).

Nothing here runs unless you use it; the SDK's default spans are unchanged.

``resource_span(name)``
    A tool span that also records this process's CPU time, resident memory and
    I/O while the span ran, under ``agentloop.system`` in its metadata.
``run_subprocess(argv)``
    Runs a command (no shell) inside a span and records its lifecycle: start,
    exit code, duration and, where the platform reports it, child CPU time.
    Arguments are not recorded: they often contain secrets.
``StreamTimer``
    Time to first token for a streamed model call, from a monotonic clock.
``cached_input_tokens(usage)``
    Cached prompt tokens from a provider usage object, when it reports them.

Every measurement is either a number or ``None`` with a reason in
``unavailable``: unsupported platform, missing permission, or a provider that
doesn't expose it. Nothing is estimated or inferred. GPU and provider queue
metrics are not collected: opaque providers don't expose them.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404 - argv only, never a shell
import sys
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from agentloop.tracer import trace_tool_call

SYSTEM_KEY = "agentloop.system"
INFERENCE_KEY = "agentloop.inference"


def _linux_rss() -> tuple[int | None, str | None]:
    try:
        with open("/proc/self/statm", encoding="ascii") as handle:
            pages = int(handle.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE"), None
    except (OSError, ValueError, IndexError):
        return None, "proc_unreadable"


def _linux_io() -> tuple[dict[str, int] | None, str | None]:
    try:
        with open("/proc/self/io", encoding="ascii") as handle:
            fields = dict(line.split(":", 1) for line in handle if ":" in line)
        return {
            "read_bytes": int(fields["read_bytes"]),
            "write_bytes": int(fields["write_bytes"]),
        }, None
    except PermissionError:
        return None, "permission_denied"
    except (OSError, KeyError, ValueError):
        return None, "proc_unreadable"


def _windows_memory() -> tuple[int | None, int | None, str | None]:
    try:
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        process = ctypes.windll.kernel32.GetCurrentProcess()  # type: ignore[attr-defined]
        if not ctypes.windll.psapi.GetProcessMemoryInfo(  # type: ignore[attr-defined]
            process, ctypes.byref(counters), counters.cb
        ):
            return None, None, "api_failed"
        return int(counters.WorkingSetSize), int(counters.PeakWorkingSetSize), None
    except (OSError, AttributeError, ImportError):
        return None, None, "api_unavailable"


def _peak_rss_unix() -> int | None:
    try:
        import resource
    except ImportError:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB, macOS bytes.
    return int(peak) if sys.platform == "darwin" else int(peak) * 1024


def sample_process() -> dict[str, Any]:
    """One snapshot of this process. Unavailable values are None, with a reason."""
    times = os.times()
    sample: dict[str, Any] = {
        "monotonic_ns": time.monotonic_ns(),
        "cpu_user_s": times.user,
        "cpu_system_s": times.system,
        "children_cpu_s": None,
        "rss_bytes": None,
        "peak_rss_bytes": None,
        "io": None,
        "unavailable": {},
    }
    if os.name == "nt":
        # Windows os.times() does not report child process CPU.
        sample["unavailable"]["children_cpu"] = "platform"
        rss, peak, reason = _windows_memory()
        sample["rss_bytes"], sample["peak_rss_bytes"] = rss, peak
        if reason:
            sample["unavailable"]["memory"] = reason
        sample["unavailable"]["io"] = "platform"
        return sample
    sample["children_cpu_s"] = times.children_user + times.children_system
    sample["peak_rss_bytes"] = _peak_rss_unix()
    if sys.platform.startswith("linux"):
        sample["rss_bytes"], reason = _linux_rss()
        if reason:
            sample["unavailable"]["rss"] = reason
        sample["io"], reason = _linux_io()
        if reason:
            sample["unavailable"]["io"] = reason
    else:
        sample["unavailable"]["rss"] = "platform"
        sample["unavailable"]["io"] = "platform"
    return sample


def _delta(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    def diff(key):
        a, b = before.get(key), after.get(key)
        return None if a is None or b is None else b - a

    io = None
    if before.get("io") and after.get("io"):
        io = {key: after["io"][key] - before["io"][key] for key in after["io"]}
    cpu_user, cpu_system = diff("cpu_user_s"), diff("cpu_system_s")
    children = diff("children_cpu_s")
    return {
        "clock": "monotonic",
        "pid": os.getpid(),
        "thread": threading.get_ident(),
        "wall_ms": round((after["monotonic_ns"] - before["monotonic_ns"]) / 1e6, 3),
        "process_cpu_ms": round((cpu_user + cpu_system) * 1000, 3),
        "children_cpu_ms": None if children is None else round(children * 1000, 3),
        "rss_start_bytes": before.get("rss_bytes"),
        "rss_end_bytes": after.get("rss_bytes"),
        "peak_rss_bytes": after.get("peak_rss_bytes"),
        "io_bytes": io,
        "unavailable": {**before.get("unavailable", {}), **after.get("unavailable", {})},
        "note": "Process-wide counters: concurrent work in other threads is included.",
    }


class _Sampler:
    """Deterministic 1-in-N sampling, so overhead can be bounded without randomness."""

    def __init__(self, every: int) -> None:
        if type(every) is not int or every < 1:
            raise ValueError("sample_every must be a positive integer")
        self.every = every
        self._count = 0
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            self._count += 1
            return (self._count - 1) % self.every == 0


_samplers: dict[tuple[str, int], _Sampler] = {}
_samplers_lock = threading.Lock()


def _sampler(name: str, every: int) -> _Sampler:
    with _samplers_lock:
        sampler = _samplers.get((name, every))
        if sampler is None:
            sampler = _samplers[(name, every)] = _Sampler(every)
        return sampler


@contextmanager
def resource_span(
    name: str, *, metadata: Mapping[str, Any] | None = None, sample_every: int = 1
) -> Iterator[dict[str, Any]]:
    """A tool span that records process resource use while it ran.

    ``sample_every=N`` measures the first of every N spans with this name; the
    others are recorded as ordinary spans with ``sampled: false``.
    """
    sampler = _sampler(name, sample_every)
    span_metadata: dict[str, Any] = dict(metadata or {})
    measure = sampler.take()
    with trace_tool_call(name, metadata=span_metadata):
        before = sample_process() if measure else None
        try:
            yield span_metadata
        finally:
            if before is not None:
                span_metadata[SYSTEM_KEY] = {"sampled": True, **_delta(before, sample_process())}
            else:
                span_metadata[SYSTEM_KEY] = {"sampled": False}


def run_subprocess(
    argv: Sequence[str],
    *,
    name: str | None = None,
    timeout: float | None = None,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run ``argv`` without a shell inside a span that records its lifecycle.

    Records the executable's base name, start and end on the monotonic clock,
    exit code, timeout and, where the platform reports it, child CPU time.
    Never records arguments, environment or output.
    """
    if isinstance(argv, str) or not argv or not all(isinstance(part, str) for part in argv):
        raise ValueError("argv must be a non-empty list of strings")
    executable = os.path.basename(argv[0])
    metadata: dict[str, Any] = {}
    with trace_tool_call(name or f"subprocess:{executable}", metadata=metadata):
        before = sample_process()
        lifecycle: dict[str, Any] = {"executable": executable, "argument_count": len(argv) - 1}
        try:
            completed = subprocess.run(  # nosec B603 - caller-supplied argv, no shell
                list(argv),
                capture_output=True,
                timeout=timeout,
                cwd=cwd,
                env=dict(env) if env is not None else None,
                check=False,
            )
            lifecycle.update(exit_code=completed.returncode, timed_out=False)
            return completed
        except subprocess.TimeoutExpired:
            lifecycle.update(exit_code=None, timed_out=True)
            raise
        except OSError as error:
            lifecycle.update(exit_code=None, timed_out=False, start_error=type(error).__name__)
            raise
        finally:
            after = sample_process()
            delta = _delta(before, after)
            lifecycle.update(
                wall_ms=delta["wall_ms"],
                children_cpu_ms=delta["children_cpu_ms"],
                unavailable={k: v for k, v in delta["unavailable"].items() if k == "children_cpu"},
            )
            metadata[SYSTEM_KEY] = {"subprocess": lifecycle, "clock": "monotonic"}


class StreamTimer:
    """Time to first token and stream completion for a streamed model call.

    Create it when the request is sent, call ``token()`` for each chunk that
    carries output, and ``finish()`` when the stream ends. ``metadata()``
    returns the measurements; a stream that never produced a token reports
    ``time_to_first_token_ms: None`` with a reason.
    """

    def __init__(self) -> None:
        self._start = time.monotonic_ns()
        self._first: int | None = None
        self._end: int | None = None
        self.chunks = 0

    def token(self) -> None:
        now = time.monotonic_ns()
        if self._first is None:
            self._first = now
        self.chunks += 1

    def finish(self) -> None:
        if self._end is None:
            self._end = time.monotonic_ns()

    def metadata(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "clock": "monotonic",
            "chunks": self.chunks,
            "time_to_first_token_ms": None
            if self._first is None
            else round((self._first - self._start) / 1e6, 3),
            "stream_complete": self._end is not None,
        }
        if self._end is not None:
            value["total_ms"] = round((self._end - self._start) / 1e6, 3)
        if self._first is None:
            value["unavailable"] = {"time_to_first_token": "no_tokens"}  # nosec B105 - model output tokens
        return {INFERENCE_KEY: value}


def cached_input_tokens(usage: Any) -> int | None:
    """Cached prompt tokens from a provider usage object or dict, if reported.

    Reads ``prompt_tokens_details.cached_tokens`` (OpenAI-style),
    ``input_tokens_details.cached_tokens`` and ``cache_read_input_tokens``
    (Anthropic-style). Returns None when the provider didn't report it, which
    is different from zero.
    """

    def get(value, key):
        if isinstance(value, Mapping):
            return value.get(key)
        return getattr(value, key, None)

    for container, key in (
        ("prompt_tokens_details", "cached_tokens"),
        ("input_tokens_details", "cached_tokens"),
        (None, "cache_read_input_tokens"),
    ):
        source = get(usage, container) if container else usage
        value = get(source, key) if source is not None else None
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


__all__ = [
    "INFERENCE_KEY",
    "SYSTEM_KEY",
    "StreamTimer",
    "cached_input_tokens",
    "resource_span",
    "run_subprocess",
    "sample_process",
]
