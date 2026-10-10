# Omnigent telemetry and session evidence

This unreleased checkout imports saved Omnigent OTLP JSON/JSONL files offline.
It uses the [bounded batch loader](OTLP_JSONL.md) and existing OTel semantics,
then adds source aliases, policy observations, separate role timing and a
session sidecar. It does not start Omnigent, a vendor CLI, collector or provider.

```bash
agentloop omnigent import-otel omnigent-export.jsonl --out runs/omnigent
agentloop omnigent import-otel omnigent-export.json --out runs/omnigent-json
agentloop omnigent inspect runs/omnigent --json-out runs/omnigent-inventory.json
uv run --frozen python examples/omnigent_otel.py --out runs/omnigent-example
```

The offline example uses owned synthetic fixtures. No live vendor, benchmark
result or measured improvement is claimed.

## Source boundary

The inspected source is Omnigent
[`a2956be0e97bb175a60b274053d836f07d494c6c`](https://github.com/omnigent-ai/omnigent/blob/a2956be0e97bb175a60b274053d836f07d494c6c/omnigent/inner/tracing.py).
Its helper emits AGENT, TOOL and GUARDRAIL spans, `session.id`,
`agent.name`/`gen_ai.agent.name`, provider/model labels and optional content.
LLM spans originate from spawned executors, so missing model spans establish
unknown model coverage, not zero inference.

Recorded aliases include agent/session/parent IDs, request/turn IDs,
harness/version/transport and skill names. The current helper does not emit all
these fields; absent values stay null. Caller/vendor extensions remain source
metadata, with unsupported strings minimized. Unsupported features are
unknown/unverified rather than behavior-verified capabilities.

The fixture matrix's original synthetic examples follow revision
`713673e9d48cf09cb71fd3a90ae45856ada288d1`/v0.17.0. These formats were checked
against the current source above. Neither revision is an inferred producer
version for an export that did not record one.

## Sessions, lineage and identities

`omnigent-sessions.json` groups observed session IDs across distinct source and
native trace IDs. It preserves explicit link targets and resolution against the
imported span inventory. Missing targets stay unresolved; links never create
native causal parent edges. Explicit ended-parent attributes lack a trace scope
and remain unresolved references even when another span happens to share the ID.
Missing cross-process continuity remains a gap.

Native parent edges still use the existing batch loader's validated within-trace
wire parentage. Session membership alone does not imply parentage, delegation,
data flow or a complete execution tree. Group IDs include explicitly supplied
job/trial context; no external session, agent or attempt identity is invented.

The sidecar has a byte hash in the inventory. Inspection rejects modified
sidecar bytes. Native observations are bound to captured trace snapshots;
post-capture mutation fails analysis/export. These checks establish consistency
with supplied bytes, not source authenticity or tamper-proof evidence.

## Policy and approval observations

Recorded `policy.action` values normalize ALLOW, DENY and ASK while retaining
the original case. Unknown actions remain UNKNOWN. Name, phase, source span,
matched tool-call IDs, recorded approval ID/resolution and callback latency
remain separate fields.

Every imported policy record has `enforcement: unverified` and
`dispatch_prevention: unknown`. A pre-tool phase label, DENY, absent tool span,
or source `enforced=true` cannot prove a callable was prevented. A matching tool
span establishes a recorded observation of that call ID; it is not a verified
pre-dispatch gate. A late DENY does not retroactively prevent work.

ASK without recorded resolution remains an explicit gap. A recorded resolution
is source evidence, not an AgentLoop approval flow. The importer does not
resolve approvals, execute policies, enable production controls or manufacture
a custom approval UI.

Omnigent marks DENY guardrail spans as ERROR. The adapter keeps that span status
while leaving the overall task execution outcome unknown. A policy refusal does
not independently establish failed task execution or external correctness.

### Transport-specific limits

[Omnigent's declarations](https://github.com/omnigent-ai/omnigent/blob/a2956be0e97bb175a60b274053d836f07d494c6c/omnigent/harness_capabilities.py)
distinguish execution modes and elicitation mechanisms. No live transport was
behavior-tested by this import workstream.

| Transport | Boundary to retain |
| --- | --- |
| SDK in process | Relevant executor callbacks may expose tool boundaries; support is vendor/version specific |
| CLI subprocess | A printed/logged policy decision does not prove dispatch interception |
| ACP subprocess | Permission/interrupt events depend on the actual server and integration |
| Native TUI | Approval mirroring is not a universal programmatic pre-tool gate |
| Native server | JSON-RPC/SSE permission handling depends on the server/version and active adapter |
| OTLP file import | Observation only; no runtime interception, denial or approval |

The [runner policy gate](https://github.com/omnigent-ai/omnigent/blob/a2956be0e97bb175a60b274053d836f07d494c6c/omnigent/runner/policy.py)
handles particular function-policy tool-call/tool-result phases. Other policy
types stay server-side, and ASK is escalated by its caller. That implementation
does not make all Omnigent transports enforceable. Registry/behavioral probes
remain the next workstream.

## Timing, usage and reports

Native JSON and HTML expose `omnigent_observations`: agent labels, session ID,
model coverage, policy evidence, lineage gaps and separate role timing. Agent
turn, model, tool, guardrail, recorded approval and host roles stay distinct.

For each role, interval union and span cumulative time are separate. Nested
agent spans with durations 10s and 4s can have a 10s union and 14s cumulative
time; role times are never added into an invented elapsed runtime. Missing bounds
remain unknown with known partial intervals explicitly identified. Provider/model
counts retain existing source qualifications; root aggregates and cache subsets
are not double counted.

Model coverage is `unknown` without model spans and `observed_partial` when
model spans exist. Neither state claims complete executor coverage. Task quality
is unavailable without the independent [trial/verifier path](HARBOR_TRIALS.md),
and telemetry-only imports cannot pass replay/study/value improvement gates.

The reserved `agentloop.omnigent` native namespace version 1.0 holds a bound
observation summary. Per-event `source.omnigent` fields retain aliases and capture
flags. Known structural attributes survive native OTel re-export without
duplicated `agentloop.metadata.*` prefixes. Source data remains externally
reported; conversion does not upgrade trust or measured provenance.

## Privacy and limits

The batch loader minimizes inputs/outputs, policy reasons, exception text,
reasoning, token IDs, credential fields and log bodies by default. Content-capture
flags describe what the source recorded; they do not enable capture in AgentLoop.
Structural names/IDs can still be private. Imported files never load referenced
code, task scripts, verifiers, media, archives or remote resources.

Common parser/path/record/trace/event/metadata limits apply. Invalid independent
records are retained by default; `--strict` stops on them. Global budget overflow
is terminal. Output remains stable on repeated imports; conflicting bytes fail
instead of overwriting. Native schema 1.1, package version 0.7.0, dependencies and
Python 3.10 support remain unchanged. These commands are not in the published
0.7.0 wheel; use a qualification-aware source checkout or future release.

```python
from agentloop.integrations.omnigent.telemetry import import_omnigent

result = import_omnigent("omnigent-export.jsonl")
result.write("runs/omnigent")
print(result.sessions())
```

```bash
uv run --frozen python -m pytest tests/test_omnigent_telemetry.py tests/test_otlp_jsonl.py tests/test_otel_interop.py tests/test_html_report.py -q -ra
```

These offline checks exercise source aliases, two child traces, sessions/links,
ALLOW/DENY/ASK, timing overlap, missing model/stream data, late spans, privacy,
namespace round trips, output consistency and CLI/HTML. They do not establish
live vendor compatibility, runtime enforcement or empirical performance gains.
