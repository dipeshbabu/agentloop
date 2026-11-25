# Compiling a concurrent schedule

Many agents call several tools one after another even when none of the calls
needs another's result. The [tool scheduler](TOOL_SCHEDULING.md) can run such
calls concurrently, bounded and in a deterministic result order, but it needs
explicit declarations for each call. `compile_schedule` reads those
declarations from a recorded run. It tells you which groups of calls could
safely overlap, and what that would plausibly save.

```python
from agentloop.schedule_compiler import compile_schedule, tool_calls

result = compile_schedule(trace, max_concurrency=4)
for group in result["groups"]:
    print(group["parent"], group["status"], group.get("predicted_gain_ms"), group.get("reasons"))
```

## What qualifies

The compiler looks at each group of sibling tool calls, meaning calls with the
same parent. It uses only what the calls declared in the
[execution IR](EXECUTION_IR.md). For every call in the group:

| Declaration | Where it comes from |
| --- | --- |
| Dependencies | `depends_on`, which may be an empty list |
| Effect | `agentloop.effects.effect`: `none`, `read` or `write` |
| State | `agentloop.effects.reads` and `writes`. A `write` effect must name what it writes. |
| Concurrency | not `parallel_safe: false` |

If any call in a group is missing one of these, declares an `external` or
unknown effect, or has unsupported evidence, the whole group abstains and lists
the reasons. Examples of unsupported evidence are two writes to the same state
key with no declared order, a cycle, or a missing dependency. The compiler never
treats calls as independent just because their timing didn't overlap.

## What a group reports

| Status | Meaning |
| --- | --- |
| `candidate` | The scheduler could overlap some calls, and the simulation predicts a gain |
| `no_gain` | Everything is declared, but dependencies and state conflicts leave nothing to overlap |
| `abstained` | Evidence is missing or unsupported. See `reasons`. |

A candidate group also includes:
- the scheduler's own plan: `effective_dependencies`, `resource_conflicts`, `serial_order` and `plan_hash`;
- `observed_ms`, the group's recorded span;
- `predicted_ms`, a simulation of the plan under `max_concurrency` with the
  observed durations;
- `critical_path_ms`;
- `predicted_gain_ms`.

A read after a write of the same state key is ordered after it automatically,
and the conflict is listed.

## Running it

`tool_calls(group, tools)` builds `ToolCall` declarations for a group, bound to
your callables by span ID. Pass them to `ToolScheduler.execute`. Results come
back in the original order. Failures stop dependent calls, and cancellation and
deadlines are cooperative. All of that is the scheduler's existing behaviour,
under the harness policies you've configured.

## Checking the prediction

`predicted_gain_ms` is a simulation from one recorded run. Measure the real
effect by timing several runs with the original serial calls and several with
the scheduled ones. Then compare with
[`compare_prediction`](TIMING_SENSITIVITY.md#checking-a-prediction):

```python
from agentloop.timing_sensitivity import compare_prediction

compare_prediction(group["predicted_gain_ms"], serial_runtimes_ms, scheduled_runtimes_ms)
```

Thread start-up, rate limits and contention on a shared service usually make
the measured gain smaller than predicted. Keep the comparison either way.

## Limits

- Declarations are trusted as given. A tool declared `read` that actually writes
  can be run concurrently with something it conflicts with.
- Only sibling tool calls are compiled. Model calls and cross-stage
  restructuring aren't changed.
- The simulation assumes the recorded durations hold under concurrency.
