# Execution IR

The execution IR is a typed, versioned description of what a traced run did:
- its operations;
- how they depend on each other;
- what each one reads, writes and affects;
- how much of that is actually known.

Later optimization passes can only rearrange work when they can show it's safe.
The IR is where that evidence lives, and where its absence is stated plainly.

It is built from the same execution graph that reports use. It doesn't replace
the trace format, and every operation is a recorded span.

```python
from agentloop.execution_ir import build_execution_ir, independence

ir = build_execution_ir(trace)
independence(ir, "span-a", "span-b")
# {"independent": False, "abstained": True,
#  "reasons": ["span-a has no declared effect", ...]}
```

## Where every property comes from

Each operation property is a pair, `{"value": ..., "basis": ...}`:

| Basis | Meaning |
| --- | --- |
| `declared` | Your integration stated it, in `agentloop.effects` or an existing declaration such as `read_only`, `parallel_safe` or `depends_on` |
| `observed` | Measured in this trace: status, timing, which span contains which |
| `inferred` | Derived by AgentLoop from ordering alone. Never authorizes anything. |
| `unknown` | No evidence. `value` is `null`. |

An operation has:

| Field | Values |
| --- | --- |
| `kind` | The operation taxonomy: `model`, `tool`, `retriever`, `memory` and so on, or `unknown` |
| `status`, `timing` | Observed. `timing` is milliseconds from the earliest observed start in the trace. |
| `instance` | The nth call with this name under the same parent. Loop iterations and retries are separate observed instances. |
| `effect` | `none`, `read`, `write` or `external` |
| `idempotent`, `deterministic` | `true` or `false` |
| `reads`, `writes` | Sorted lists of state keys |
| `authorization_scope`, `freshness_scope` | Short labels, for example `user:123` or `request` |
| `parallel_safe` | The existing declaration, if any |
| `source` | The [source receipt](SOURCE_PROVENANCE.md), if one was captured |

## Declaring effects

Add an `agentloop.effects` object to a span's metadata:

```python
with agentloop.trace_tool_call(
    "save_cart",
    metadata={
        "depends_on": ["load_cart_span_id"],
        "agentloop.effects": {
            "effect": "write",
            "idempotent": True,
            "writes": ["cart"],
            "authorization_scope": "user",
        },
    },
):
    ...
```

`read_only: true` already counts as a declared `read` effect, and
`read_only: false` as a declared `write`. Malformed or contradictory
declarations are not guessed at. That covers an unknown effect name, a
non-boolean flag, a `read` effect that declares writes, or `read_only: true`
with `effect: "write"`. The property stays `unknown`, and the problem is listed
under `unsupported`.

## Edges

| Kind | Basis | Meaning |
| --- | --- | --- |
| `contains` | observed | The parent span contains the child span |
| `data` | declared | The target declares `depends_on` the source, as plain metadata or in its `agentloop.stage` declaration |
| `state` | declared | The source writes a state key that the target later reads or writes |
| `sequence` | inferred | Root spans ran one after another and nothing was declared. This is order only, not causation. |

An operation that declares its dependencies, even as an empty list, gets no
inferred `sequence` edges into it.

## Missing, invalid and unsupported evidence

The IR keeps going when evidence is incomplete, and records each problem under
`unsupported` with the operation it affects. That includes:
- duplicate span IDs, and parents or dependencies that aren't in the trace;
- dependency cycles;
- malformed `depends_on`, or a stage declaration that disagrees with it;
- malformed effect declarations;
- two operations that write the same state key with no declared order between
  them, which are also listed under `conflicts`.

Spans without timing have `timing: unknown`. Dynamic execution is represented
by what was observed: each loop iteration is its own operation. The IR doesn't
describe iterations that didn't run.

## Independence

`independence(ir, a, b)` says two operations may be reordered or run concurrently
only when all of these hold:

- Both declare an effect of `none` or `read`, and neither declares writes.
- No declared or observed path connects them: data, state or containment.
- Neither has unsupported evidence.

When the only thing connecting them is an inferred sequence edge, or when either
effect is unknown, the answer is `independent: false` with `abstained: true`. An
abstention means "not established", not "dependent". Inferred edges and
inferred properties can never authorize scheduling or an effectful change.

## Versions and storage

The IR's schema is `agentloop.ir/v1`. Use `ir_to_json(ir)` for canonical JSON,
with sorted keys and no whitespace, and `ir_digest(ir)` for its SHA-256. The same
trace gives the same digest regardless of event order.

`load_execution_ir(document)` reads:
- a stored `agentloop.ir/v1` document;
- a legacy report graph (`report["graph"]`), which it migrates. Its
  `input_schema` is then `agentloop.graph/legacy`.

It refuses any other schema with `IRVersionError` rather than guessing. Traces
recorded before this feature produce valid IR in which every effect property is
`unknown`.

## Limits

- Declarations are the integration's claims. AgentLoop records them; it can't
  verify that a tool declared `read` really has no side effects.
- State keys are compared as exact strings. Two names for the same resource
  look independent.
- The IR describes one recorded run. A branch that wasn't taken isn't in it.
