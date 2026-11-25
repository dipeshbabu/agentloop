# Timing sensitivity

Before changing an agent to make it faster, it helps to know which operation is
worth changing. Making a call that isn't on the critical path faster saves
nothing. Making the slowest step faster may only move the bottleneck elsewhere.

`timing_sensitivity` simulates that on a recorded run. `compare_prediction`
then checks the simulation against what actually happened when you made the
change. The two results are labelled separately because they are different
kinds of evidence. A simulation is a hypothesis about one run's structure; only
the paired runs measure an effect.

```python
from agentloop.timing_sensitivity import compare_prediction, timing_sensitivity

result = timing_sensitivity(trace, scale=0.5)
result["critical_path"]           # ["plan", "search", "answer"]
for op in result["operations"]:   # largest simulated gain first
    op["id"], op["gain_if_removed_ms"], op["gain_if_scaled_ms"], op["slack_ms"]
```

## What it reports

For every leaf operation:

| Field | Meaning |
| --- | --- |
| `on_critical_path`, `slack_ms` | Whether delaying it would delay the run, and by how much it could slip without doing so |
| `gain_if_removed_ms` | Simulated runtime saved if it took no time at all. This is an upper bound for any speed-up of it. |
| `gain_if_scaled_ms` | Simulated runtime saved if it took `scale` times as long (default 0.5) |
| `critical_path_after_removal`, `bottleneck_after_removal` | Where the critical path and its longest step move once it costs nothing |

Parents are marked `composite` and get no gain. To speed up a parent, change
one of its children.

## The model

The simulation uses the [execution IR](EXECUTION_IR.md) and holds everything it
can't justify changing at its observed value:

- A leaf operation's duration is what an intervention changes.
- A declared `data` or `state` dependency, or an inferred `sequence` between
  root spans, starts the target after its last predecessor ends. The observed
  wait after that predecessor is kept.
- A parent ends after each child, plus the observed time between that child's
  end and the parent's end.
- An operation with no declared or inferred predecessor keeps its observed start
  offset from its parent. Two nested steps that ran one after the other but
  didn't declare a dependency are therefore treated as unrelated. Making the
  first one faster doesn't move the second. Declare `depends_on` to let
  speed-ups propagate.

Queueing and scheduling delays don't shrink, and the model assumes unlimited
concurrency at the observed level. Real gains can be smaller when operations
contend for a rate limit, a connection pool or a CPU.

## When it abstains

The result has `status: "abstained"` and a list of reasons when:

- any operation's timing is missing, or its timestamps disagree with its recorded duration;
- a child runs outside its parent, or a dependent operation starts before its
  dependency ends (more than 1 ms of clock disagreement);
- the IR reports unsupported evidence, such as a missing parent or dependency,
  a cycle, a malformed declaration or a write conflict;
- the model can't reproduce the observed runtime to within 1% plus 1 ms, which
  means the run's structure isn't captured well enough to simulate.

## Checking a prediction

Make the change, then record several runs of the baseline and several of the
candidate on the same workload:

```python
check = compare_prediction(
    predicted_gain_ms=op["gain_if_scaled_ms"],
    baseline_runtimes_ms=[...],
    candidate_runtimes_ms=[...],
)
check["measured_gain_ms"], check["measured_interval_ms"]
check["prediction_error_ms"], check["prediction_within_interval"]
```

- The measured gain is the difference between the two groups' median runtimes.
- Its interval comes from resampling each group with a fixed seed: 2,000
  resamples and 95% by default, so the same inputs always give the same result.
- `measured_effect_detected` is true only when the interval excludes zero.
- The comparison abstains with fewer than two valid runs per group. Negative or
  non-finite runtimes are dropped and counted.

Run a negative control as well: change an operation the simulation says is off
the critical path. Its predicted gain is zero, and its measured interval should
include zero. If it doesn't, something other than your change is moving the
runtime.

## Limits

- One recorded run is one structure. A different input can take a different path.
- Declared dependencies are trusted as stated, and undeclared ones are assumed
  absent.
- Agreement between prediction and measurement on a few runs doesn't establish
  that the model is right in general. Keep the comparisons and their intervals
  as evidence, including the ones that disagree.
