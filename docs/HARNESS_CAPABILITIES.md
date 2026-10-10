# Declared and behavior-verified capabilities

This unreleased source checkout enumerates versioned adapter descriptions and
keeps declarations separate from scoped observations. It does not execute a
vendor runtime, discover plugins or enable harness policies during inspection.

```bash
agentloop harness list-adapters
agentloop harness capabilities --adapter python-wrapped --json-out runs/python-capabilities.json
agentloop harness capabilities --adapter omnigent-otel
```

The registry currently describes four built-in adapters:

| Adapter | Kind and scope |
| --- | --- |
| `python-wrapped` | Existing Python `AdapterCapabilities`; explicitly wrapped callable boundaries |
| `langgraph-1.2.11` | Existing `LangGraphHarness(boundaries=ALL)` declarations; requires explicit boundary registration at runtime |
| `harbor-atif` | Read-only ATIF source projection; no runtime interception |
| `omnigent-otel` | Read-only OTLP/session/policy observations; no dispatch control |

LangGraph enumeration constructs its import-safe capability descriptor without
loading the LangGraph SDK. This describes the all-boundaries configuration,
not the narrower default graph configuration or every vendor-internal call.
Version 1.2.11 is the target upstream version; enumeration does not assert that
it is installed or behavior-tested.

## Manifest 1.0

`AdapterManifest` records identity/version, upstream harness/version, transport,
control/observation kind, observation/declaration sources, code fingerprint and
typed capability declarations. Each declaration records its name, boolean/null
support, explicit boundary set, reason and live-probe requirement. Unknown
structural fields and duplicate dimensions fail. Canonical manifest SHA-256
binds these exact declarations; source fingerprints identify available built-in
module bytes and can be unavailable in a frozen distribution.

Dimensions cover observation, dispatch enforcement, callable lifecycle and
compatibility. Declarations derive supported model/tool/completion hooks and
sync/async/generator lifecycles from the existing contracts. No universal
pre-request gate, arbitrary interruption, model override, approval, compaction,
resume or fork is inferred from those contracts. Absent declarations stay null.

## Observation and state

`CapabilityObservation` records capability/verdict, versioned probe identity,
tested adapter version/transport, manifest hash, relative evidence file/hash,
environment fingerprint, timezone-qualified test timestamp and tested boundaries.
Its source distinguishes offline behavioral, live behavioral and external reports.

| State | Meaning |
| --- | --- |
| `declared_supported` | Support is declared; no current qualifying probe |
| `declared_unsupported` | The adapter explicitly disclaims support |
| `verified_supported` | A trusted current scoped behavioral probe passed |
| `verified_failed` | A trusted current scoped behavioral probe failed |
| `unknown` | Missing declaration/evidence, stale identity, wrong scope or external-only result |
| `skipped` | Probe did not run; a reason is mandatory |

A declaration of true with a failed probe produces drift. A false declaration
with a passing probe also produces drift; the original declaration is retained.
Missing probe/version/evidence/environment/timestamp, changed manifest, transport
or boundary mismatch cannot produce verified support. A credential/SDK skip is
neither a pass nor evidence of unsupported behavior.

Model override, approval, context handling, resume and fork need a relevant live
probe. An offline fake cannot upgrade those states. A successful Python wrapper
probe does not transfer to ACP, native TUI, native server or another SDK version.

## Trust and runtime controls

Catalog files are bounded strict JSON data. Loading a manifest never imports
its adapter name, follows entry points or runs source code. No plugin discovery
is currently justified; an explicitly installed future Python plugin would be
fully trusted code, separate from artifact parsing.

Observation serialization preserves recorded claims. Deserializing a historical
report gives external evidence, not a fresh trusted runtime probe. The internal
probe bridge is reserved for executable driver outcomes; it is not an
authenticity guarantee against malicious code in the current Python process.
Hashes establish supplied-byte consistency, not honesty.

`require_verified_boundary` gates new registry-dependent controls on current
built-in, control-kind, declared support and trusted scoped probe evidence. A
read-only source or catalog cannot enable enforcement. The LangGraph gate also
checks the currently installed upstream version. Existing wrapper configuration
and enforcement APIs retain their behavior; registry inspection never switches
their disabled/shadow/enforce mode.

Instrumentation coverage is explicit boundaries only. Hidden SDK calls,
unwrapped tools and externally running agents are outside that claim. A recorded
policy decision or approval event is not a behavioral denial probe.

## Python and validation

```python
from agentloop.interoperability.registry import capability_report, get_adapter

report = capability_report(get_adapter("python-wrapped"))
assert not report["drift"]
# This is a declaration report, not proof that any behavioral probe ran.
```

```bash
uv run --frozen python -m pytest tests/test_capability_registry.py tests/test_harness.py tests/test_enforcement_conformance.py -q -ra
```

Tests cover scoped declarations, both drift directions, missing/stale evidence,
serialization, data-only loading, unsupported enforcement, import safety and
existing wrapper defaults. The [executable offline bench](HARNESS_BENCH.md)
provides scoped wrapper probes. No live provider/transport was exercised here.

The design follows the declaration/observation separation in Omnigent
[`a2956be0e97bb175a60b274053d836f07d494c6c`](https://github.com/omnigent-ai/omnigent/blob/a2956be0e97bb175a60b274053d836f07d494c6c/omnigent/harness_capabilities.py),
using AgentLoop's existing implementations. No upstream code was copied, optional
runtime dependency added, schema/release version changed or production control
enabled. Core Python 3.10 remains supported; these commands are not in the
published 0.7.0 wheel.
