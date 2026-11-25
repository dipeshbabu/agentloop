# System and inference telemetry

Spans tell you how long each step took. Sometimes you also need to know why:
- Was the step busy on the CPU, waiting on the network, or growing memory?
- Did a subprocess fail?
- Was the model slow to start answering, or slow to finish?

`agentloop.system_telemetry` adds those measurements where you ask for them,
using only the standard library. Nothing changes for spans you don't wrap.

```python
from agentloop.system_telemetry import (
    StreamTimer, cached_input_tokens, resource_span, run_subprocess,
)

with resource_span("build_index"):
    build_index()

run_subprocess(["git", "status"], timeout=30)

timer = StreamTimer()
for chunk in client.stream(...):
    if chunk.text:
        timer.token()
timer.finish()
metadata = {**timer.metadata(), "cached_input_tokens": cached_input_tokens(response.usage)}
```

## Process resources: `resource_span`

A tool span that records this process's resource use while it ran, under
`agentloop.system`:

| Field | Meaning |
| --- | --- |
| `wall_ms` | Elapsed time on the monotonic clock |
| `process_cpu_ms` | User plus system CPU time used by the whole process |
| `children_cpu_ms` | CPU used by finished child processes. Not reported on Windows. |
| `rss_start_bytes`, `rss_end_bytes` | Resident memory at the start and end. Linux and Windows. |
| `peak_rss_bytes` | The process's peak resident memory so far |
| `io_bytes` | Bytes read and written. Linux only, from `/proc/self/io`. |
| `unavailable` | Why a value is missing: `platform`, `permission_denied`, `proc_unreadable`, `api_failed` |
| `pid`, `thread` | Which process and thread recorded it |

The counters cover the whole process, so work in other threads during the span
is included. Use these numbers to see what a step was doing, not as exact
per-step accounting in a busy multithreaded process.

`sample_every=N` measures the first of every N spans with the same name. The
rest are recorded with `sampled: false`.

## Subprocesses: `run_subprocess`

Runs a command without a shell, inside a span. The span records:
- the executable's base name and the number of arguments;
- the exit code, or whether it timed out or failed to start;
- elapsed time and, where the platform reports it, child CPU time.

It doesn't record the arguments, environment or output, because they often
contain secrets. It returns the usual `CompletedProcess` and raises the usual
exceptions.

## Model calls

`StreamTimer` measures a streamed call on the monotonic clock:
- time to first token, how many chunks arrived, and the total time;
- whether the stream finished. A stream that never produced a token reports
  `time_to_first_token_ms: null` with a reason, not zero.

`cached_input_tokens(usage)` reads cached prompt tokens where the provider
reports them: OpenAI-style `prompt_tokens_details.cached_tokens` and
`input_tokens_details.cached_tokens`, or Anthropic-style
`cache_read_input_tokens`. It returns `None` when the provider didn't say,
which is different from zero. Store the result as `cached_input_tokens` in the
model span's metadata. That key is the one AgentLoop's OpenTelemetry import
already uses.

## Not collected

GPU utilisation and provider queue time are not collected or estimated. Hosted
providers don't expose them, and guessing them from latency would be invented
data. Local inference servers that publish real metrics can be added when
there's a concrete need.

## Overhead

On a Windows laptop with Python 3.13, one process sample took about 30 µs, and
a measured span takes two. `tests/test_system_telemetry.py` checks the added
cost on short (empty) and longer (20 ms) tasks, and keeps it far below 5 ms per
span. For very hot loops, use `sample_every`.

## Privacy

Measurements contain process IDs, thread IDs, byte counts and timings. They
contain no file names, command arguments or environment variables.
