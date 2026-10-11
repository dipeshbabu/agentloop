# Hermes Agent tracing

AgentLoop observes [Hermes Agent](https://hermes-agent.nousresearch.com/) through
its supported plugin hooks. It writes one native JSON trace per turn, which you
can analyze, store, or compare with the usual AgentLoop commands.

This integration is available from the current source checkout. It has not yet
been published in the 0.1.0 Python release. The directory plugin uses only Python's
standard library and does not add dependencies to Hermes's managed environment.
The tested hook contract is Hermes **0.21.6**, revision
[`818c13be1dc4`](https://github.com/NousResearch/hermes-agent/tree/818c13be1dc4fd28987e1e881a9408224afd4535).

## Create and enable the plugin

From an AgentLoop checkout with `uv sync --locked` completed:

```bash
uv run --frozen agentloop hermes export-plugin runs/hermes-agentloop
```

This creates a fresh directory containing `plugin.yaml`, the observer, its loader,
and the Apache license. It refuses to replace an existing directory.

Copy that directory to `plugins/agentloop` under your **active Hermes profile home**,
then enable it:

```bash
hermes plugins enable agentloop
hermes plugins list
```

Restart the Hermes CLI or gateway after enabling it. Use the profile selected by
your Hermes installation; copying files into a different profile does not enable
capture there. The plugin does not install Hermes or configure a model provider.

By default, each completed turn writes a file such as
`agentloop/traces/run_hermes_<id>.json` under that profile home. Analyze a file with
the AgentLoop checkout:

```bash
uv run --frozen agentloop analyze /path/to/profile/agentloop/traces/run_hermes_ID.json --html runs/hermes-report.html
```

The plugin's own code needs no AgentLoop installation inside Hermes. Owners of
an unmanaged Python environment can also install the current AgentLoop source
there and enable its `hermes_agent.plugins` entry point. Use the directory plugin
with managed Hermes installations.

## What the trace contains

| Evidence | Capture behavior |
| --- | --- |
| Turn boundary and outcome | Completed, failed, interrupted, or incomplete turns remain explicit. |
| Model requests | Each main-loop provider attempt retains host timestamps, duration, model identity, and reported usage. |
| Retries | A reported retry records a separate retry event; failed attempts remain in the trace. |
| Tool calls | Tool name, host-reported status and duration, with an interval inferred from callback time. |
| Delegation | Parent/child session links; child usage is not copied into the parent's totals. |
| Missing evidence | Dropped events, unfinished requests, and missing start/timing data mark capture incomplete. |

Hermes reports uncached input, cache-read, and cache-write counts separately.
AgentLoop combines them into prompt tokens and preserves the buckets. Reasoning
tokens already belong to output usage and are not added again. Missing or invalid
usage stays unavailable. AgentLoop's configured model-cost estimates do not
reproduce Hermes's cache-specific prices or subscription billing.

The recorder covers main-loop request hooks, including normalized streaming
responses. Auxiliary calls such as titling, compression, and MoA calls have a
separate Hermes hook family and are outside this capture. It cannot reconstruct
events omitted by Hermes or certify task correctness. Use your own quality
fixtures when comparing candidate runs.

## Settings and retained content

Set options in your profile's `config.yaml`:

```yaml
plugins:
  entries:
    agentloop:
      settings:
        export_dir: agentloop/traces
        capture_content: false
        max_active_turns: 64
        max_events: 2048
```

Relative export paths resolve under the profile home. Absolute paths are also
supported. Output text is off by default. Setting `capture_content: true` retains
tool/model/final output text up to 10,000 characters; oversized outputs are omitted,
not shortened into quality evidence. Prompts, arguments, histories, request bodies,
base URLs, and raw error messages remain excluded. Names and opaque host IDs still
need review before sharing.

Callbacks return `None`; they do not inject prompts, approve tools, change results,
or make network calls. Teardown and unload flush unfinished turns as incomplete.
Failed exports are retried on flush, with a bounded buffer of sixteen traces. The
recorder uses atomic publication through a hard link in the export directory;
the filesystem must support hard links. Existing trace files are never replaced.
Export failures produce a local log warning and do not stop Hermes execution.

## Programmatic registration

Inside your own Hermes plugin, with current AgentLoop source installed:

```python
from agentloop.integrations.hermes import register_hermes

def register(ctx):
    recorder = register_hermes(ctx)
```

`agentloop.instrument(ctx, integration="hermes")` uses the same registration path.
Registration is idempotent for the supplied context. For an explicitly chosen
output directory, construct `HermesRecorder(path)` and call `recorder.register(ctx)`.
`recorder.summary()` reports active turns, unscoped events, and export failures.

See Hermes's [plugin guide](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins)
and [hook reference](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks)
for the host's configuration and delivery behavior.
