# Source provenance

Patch plans point a reviewer at the code behind a finding. Without help, the
planner matches span names to function names in your checkout. That breaks when
several files or classes define the same name, which is common: every agent has
a `search` or a `run`. Source provenance records exactly which definition
produced each span, so a plan can target it and say when it can't.

It is off by default and records no source text or absolute paths.

## Turn it on

For one function:

```python
import agentloop


@agentloop.traceable(capture_source=True)
def search(query: str):
    ...
```

`trace_tool(...)` and `trace_model(...)` take the same argument. To turn it on for
every decorated function without editing code, set the environment variable
before your modules are imported:

```bash
export AGENTLOOP_CAPTURE_SOURCE=1
```

An explicit `capture_source=False` wins over the environment variable.

## What a span records

Each span from a decorated function gets an `agentloop.source` entry in its
metadata:

```json
{
  "schema": "agentloop.source/v1",
  "status": "captured",
  "path": "agents/research.py",
  "qualname": "Researcher.search",
  "module": "agents.research",
  "line_start": 41,
  "line_end": 58,
  "content_sha256": "9f2c…",
  "revision": "optional"
}
```

| Field | Meaning |
| --- | --- |
| `path` | Path relative to the source root, always with `/`. Never absolute. |
| `qualname` | Python's qualified name, such as `Researcher.search` or `build.<locals>.step`. |
| `line_start`, `line_end` | The definition's lines. `line_start` includes decorators. |
| `content_sha256` | SHA-256 of the whole file as UTF-8 text with newlines normalized to `\n`, so Windows and Unix checkouts of the same commit match. |
| `revision` | Copied from `AGENTLOOP_SOURCE_REVISION`, for example a CI commit SHA. Informational only: matching uses the digest. |

The source root is `AGENTLOOP_SOURCE_ROOT` if set, otherwise the nearest
directory containing `.git` above the file, otherwise the current directory.

Some functions can't be located: code run with `exec` or `python -c`, notebook
cells, built-ins, and files outside the source root such as installed packages.
They still get a receipt with their qualified name and a `status` of
`unavailable` or `outside_root`, but no path.

The receipt is computed once, when the function is decorated. Each call copies
a small dictionary.

## How patch plans use it

`agentloop patch-plan` and `build_patch_plan(...)` resolve each evidence span's
receipt against the checkout you pass. Every patch plan lists the result under
`source_locations`, grouped by outcome with the span IDs it covers:

| Status | Meaning | Rewrite eligible |
| --- | --- | --- |
| `resolved` | Same path, same file digest and exactly one definition at the recorded lines | Yes |
| `moved` | The file is missing at its recorded path, but exactly one file has the same digest | No |
| `stale` | The file at the recorded path has changed since the trace was recorded | No |
| `ambiguous` | Several identical copies of the file, or several definitions at matching lines | No |
| `file_missing` | No file at the recorded path and no copy elsewhere | No |
| `symbol_missing` | The file matches but has no such qualified name at those lines | No |
| `unverified` | The receipt has no digest, for example OpenTelemetry `code.*` attributes from another tool | No |
| `invalid` | The path is absolute, escapes the checkout, or the name is malformed | No |
| `outside_root` | Recorded without a path because the file is outside the source root | No |
| `unavailable` | Recorded without a path, or the span has no receipt at all, as in every trace recorded before this feature | No |

Files backed by a receipt are listed first. A `resolved` file has confidence
`exact` and `rewrite_eligible: true`. A plan is `rewrite_eligible` only if at
least one of its files is. Name and text matches are still listed after them as
leads, and are never eligible. Traces without receipts plan exactly as before,
and none of their files are eligible.

For `moved`, `stale` and `unverified` the plan still shows where the definition
appears to be now, so a reviewer can look. Re-record the trace on the current
checkout to make it eligible again.

## Other trace formats

- Native JSON and the HTTP API keep receipts as ordinary span metadata.
- OTLP export and import keep them through the `agentloop.metadata.` attributes.
- Spans from other tools that carry OpenTelemetry code attributes are read as
  `unverified` receipts. These are `code.file.path` or `code.filepath`,
  `code.function.name` or `code.function` (with `code.namespace`), and
  `code.line.number` or `code.lineno`.
- The TypeScript SDK doesn't capture receipts. A TypeScript integration can pass
  the same `agentloop.source` object in span metadata itself. Without a digest it
  resolves as `unverified`.

Context managers such as `trace_tool_call(...)` have no function to point at, so
they record no receipt unless you pass one in `metadata`. Build one with
`agentloop.source_provenance.capture_source(func)`.

## Measured precision and overhead

`scripts/benchmark_source_provenance.py` builds a throwaway repository of 20
modules. Each module defines both a `search` function and a `Client.search`
method. The script traces three calls to one known definition per trial and asks
the planner where to look.

| Mapping | First candidate is the traced file | Rewrite-eligible candidates that are correct |
| --- | --- | --- |
| Name matching (no receipts) | 2 / 40 | none eligible |
| Source receipts | 40 / 40 | 40 / 40 |

On one Windows laptop with Python 3.13, capture added about 0.6 µs to a traced
call that takes about 12 µs. Building a receipt took about 0.5 ms, once per
decorated function. Your numbers will differ: run the script to measure your own.

```bash
uv run python scripts/benchmark_source_provenance.py --modules 20 --calls 20000
```

This fixture is deliberately adversarial for name matching. It shows the failure
mode, not the accuracy you'll see in a typical repository.

## Turning it off

Remove `capture_source=True`, or unset `AGENTLOOP_CAPTURE_SOURCE`. Receipts
already stored in traces are ignored by older AgentLoop versions and do no harm.
To strip them from a stored trace, delete the `agentloop.source` key from each
event's metadata.
