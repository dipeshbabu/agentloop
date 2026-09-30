# First-use telemetry paths

These are **Unreleased source-checkout APIs**. Start with telemetry you already
export, or install an adapter on an existing application object. AgentLoop does
not claim to see operations that an SDK or framework does not expose.

## Existing telemetry: no business-logic changes

Export an existing OTLP JSON batch from your tracing system, then run:

```bash
agentloop onboard existing-telemetry.json --format otlp \
  --expected-operation model --expected-operation tool --out runs/onboarding.json
```

Supported GenAI, OpenInference and MCP conventions use the existing
[OpenTelemetry adapter](INTEGRATIONS.md). Each source trace ID remains a separate native
trace; imported evaluation logs do not become execution spans. The command
validates each trace before running the existing analysis rules. It writes a
summary with operation counts, usage/cost completeness and finding estimates.
No application code or remote service is invoked. This is a local JSON import,
not a newly installed OTLP receiver or exporter.

For an offline example using the maintained OpenInference conformance fixture:

```python
import json
from pathlib import Path
from agentloop.onboarding import onboard

fixture = json.loads(Path("tests/fixtures/telemetry/openinference.json").read_text())
result = onboard(fixture["payload"], format="otlp", expected_operations=("model",))
print(result["trace_count"], result["traces"][0]["validation"])
```

This fixture is synthetic, version-pinned telemetry. It demonstrates import and
validation, not application quality or optimization gains. Raw arrays of span
objects and supported OTLP JSON resource/scope envelopes use the same adapter.
Native AgentLoop traces use `--format native` (the default).

## Existing model client in an agent workload

Install the optional SDK integration support, then add one explicit startup call
and one host execution boundary. Existing business calls use the same client:

```python
import agentloop
from agentloop import WorkflowInfo, trace_workflow

agentloop.init(export_dir="runs")
agentloop.instrument(client, integration="openai")

with trace_workflow("agent-job", workflow=WorkflowInfo("agent-job", "v1")):
    result = existing_agent.run()  # Uses the already-created client.
```

The caller creates `client` and `existing_agent` as before. The supported client
methods are `responses.create` and `chat.completions.create`, when present.
Sync/async calls and sync/async streams reuse the existing adapter; streams must
be consumed or closed while the host execution is active if their work is to fit
that boundary. Final usage exists only when the SDK supplies it. Calls made with
no active trace remain ordinary calls and are not recorded. Prompts/responses
are not captured by this adapter, but error text and labels can be sensitive.

Other explicit targets are `integration="langgraph_builder"` (before registering
nodes) and `integration="crewai_task"` (supported task execution methods). They
reuse their existing tool-span wrappers. Node/task hooks do not infer model
usage or generator-consumption lifecycles; instrument the actual model client as
well. Existing decorators and context managers remain supported. This entrypoint
does not instrument arbitrary libraries or compiled graphs automatically.

`AGENTLOOP_INSTRUMENTATION_ENABLED=true|false|1|0` can configure this explicit
startup call; `enabled=True/False` overrides the environment. Setting an
environment variable alone installs nothing. Disabling installation does not
remove wrappers already installed on an object: configure fresh objects at
startup. Unsupported targets fail explicitly. Repeated enabled installation is
idempotent. The deprecated `auto_instrument()` remains a detection-only alias;
use `instrument()` to install an adapter and `detect_integrations()` to inspect
package availability.

## Generic decision pipeline

A pipeline can retain ordinary control flow and annotate its meaningful stage:

```python
import agentloop
from agentloop import StageInfo, WorkflowInfo, trace_operation, trace_workflow

agentloop.init(export_dir="runs")
with trace_workflow("decision-batch", workflow=WorkflowInfo("decision-batch", "v1")):
    with trace_operation("classify", kind="classifier",
                         stage=StageInfo("classify", "v1", kind="classifier")):
        decisions = existing_classifier.predict(rows)
```

Then run `agentloop onboard runs/RUN_ID.json --expected-operation classifier`.
Alternatively, import the pipeline's existing OTLP/OpenInference export with the
first recipe. The classifier wrapper reports its elapsed work; it does not
invent provider usage, a monetary cost or independent correctness scores.
Model-backed classifiers should instrument the model call too. Use the existing
quality contracts to test the decisions separately.

## Interpreting first-use validation

`ready` means no detected schema/coverage warnings in the supplied evidence;
`partial` means recorded evidence has stated limitations; `invalid` blocks
analysis. Missing parents, parent cycles, absent/mixed source trace IDs and invalid
elapsed times are invalid. Warnings include absent
execution spans, unsupported operation kinds, missing expected kinds, absent or
estimated usage, missing model identity, incomplete trace timing, spans outside
the boundary and captured input/output/error text. Metadata/labels are always
marked for privacy review when present. Privacy is never certified automatically.

Expected operation kinds are explicit host expectations, not inferred from a
framework name. Without those expectations, omitted work can be invisible. No
warning-free result proves complete instrumentation or task quality. A completed
execution is distinct from a correct answer.

Analysis is skipped for invalid or lossy retained evidence, empty traces, or more
than 2,000 spans per trace. Use `--no-analyze` for validation only and
[incremental aggregation](INCREMENTAL_AGGREGATION.md) for large batches. Input
files have a 16 MiB bound. Invalid/empty captures exit nonzero; partial captures
write their explicit warnings and exit successfully. Output cannot overwrite
input telemetry.

The onboarding JSON excludes raw input/output/error text, arbitrary metadata,
names and model labels. It retains source hashes, run/span identifiers, aggregate
metrics and selected finding fields. Use opaque run/span IDs; hashing is not
anonymity against guessing. The command does not export a full native trace or
promise replayability. Existing import APIs can produce native traces when you
intend to retain that evidence; apply [retention](TRACE_RETENTION.md) before
sharing. Finding savings remain estimates requiring a paired quality/performance
experiment.

## Setup steps and overhead

Run the offline benchmark from the source checkout:

```bash
uv run --frozen python examples/onboarding_benchmark.py --out runs/onboarding-benchmark.json
```

It records raw repetitions, source hashes, environment and descriptive summaries
for an unchanged local SDK-shaped callback, the installed wrapper without a
trace, active recording with a host boundary, and parsed OpenInference payload
import. It rotates measurement order and excludes two warm-up rounds. It counts
three SDK setup steps (install, initialize adapter, host boundary) and two existing
telemetry steps (export, onboard), excluding application-specific exporter setup.

Results measure local adapter/import work on synthetic maintained examples, not
provider/network latency, production agent overhead or task-quality improvement.
There are no paid calls and no timing threshold in CI. The benchmark refuses to
overwrite an existing result. The [September 2026 measurement](../research/onboarding-2026-09/README.md)
retains raw repetitions, outliers, normalized source hashes and setup definitions.
