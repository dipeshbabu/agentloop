# Optimization search

Once you have several candidate interventions, the next question is which ones
to use together. Each might be a reuse pass, a schedule or a cheaper model. Some
help on their own but conflict with each other. Some save time but cost answer
quality. `agentloop.optimization_search` runs a small, bounded search over them.
It returns a decision for you to review. It never applies or deploys anything.

```python
from agentloop.optimization_search import (
    Intervention, Objective, SearchBudget, SearchSpec, run_search,
)

spec = SearchSpec(
    interventions=[
        Intervention("reuse"),
        Intervention("parallel_tools"),
        Intervention("smaller_model"),
        Intervention("full_cache", conflicts_with={"reuse"}),
    ],
    tuning_cases=["t01", "t02", "t03"],
    held_out_cases=["h01", "h02"],
    objectives=[Objective("runtime_ms", "min"), Objective("cost_usd", "min")],
    quality_floor=0.0,
    budget=SearchBudget(max_attempts=80, max_elapsed_s=1800, max_cost_usd=5.0),
    seed=7,
)
report = run_search(spec, evaluate)
report["decision"]
```

You write `evaluate(interventions, case_id)`. It runs your agent on one case
with those interventions applied, for example through an
[experiment session](OPTIMIZATION_EXPERIMENTS.md) or the
[transformation registry](TRANSFORMATIONS.md), and returns:

```python
{"quality": 0.92, "runtime_ms": 1830.0, "cost_usd": 0.013}
```

`quality` is required and must come from your own independent check. Objective
metrics may be `None` when unknown. Return `{"status": "error", ...}` for a run
that failed.

## What the search does

1. **Freezes the specification.** Tuning and held-out cases must not overlap,
   by ID or, when you pass `case_digests`, by content. Quality is a constraint,
   never an objective.
2. **Runs the baseline**, with no interventions, on every tuning case.
3. **Runs each single intervention** on every tuning case. Tie order is shuffled
   with your seed, so runs are reproducible.
4. **Combines greedily.** It starts from the best single intervention that
   keeps quality, then adds the next one while the primary objective (the first
   listed) improves. It only combines interventions with no declared conflict,
   up to `max_combination_size`.
5. **Evaluates once on held-out cases.** The selected combination and the
   baseline run on cases the search never saw.

An intervention keeps quality when no tuning case's quality falls more than
`quality_floor` below the baseline's on that case, and every case ran
successfully. Unknown objective values never win.

## Budgets and stopping

| Limit | Stops when |
| --- | --- |
| `max_attempts` | That many case evaluations have run |
| `max_elapsed_s` | That much time has passed, by a monotonic clock |
| `max_cost_usd` | Measured cost reaches it, or any attempt reports unknown cost. A budget that can't be checked isn't enforced by guessing. |
| `cancel` event | You set it. Checked before and after each attempt. |
| `KeyboardInterrupt` | Raised inside `evaluate`. The attempt is recorded as `interrupted`. |

A stopped search reports `stop_reason` and returns a decision of `incomplete`.

## The report

| Field | Contents |
| --- | --- |
| `attempts` | Every evaluation in order, with its phase, interventions, case, status and metrics. Includes the baseline, failures, invalid results and interruptions. |
| `candidates` | Each combination's mean metrics, whether it kept quality, and which cases regressed or failed |
| `pareto` | Combinations that keep quality and aren't beaten on every objective |
| `held_out` | Baseline and selected results on held-out cases, and the net gain per objective |
| `decision` | `recommend_for_review`, `no_improvement`, `rejected` (held-out quality fell, or the gain didn't hold) or `incomplete`. A recommendation includes the combination's measured interaction: its gain minus the sum of its parts' gains. |
| `overhead` | Attempts, elapsed time and evaluation time: the cost of searching |
| `report_sha256` | A digest of everything except wall-clock times. The same seed and results give the same digest. |
| `deployed` | Always `false` |

## Limits

- Greedy search can miss a combination whose parts look poor alone but work
  well together. Raise `max_combination_size`, or evaluate that combination
  directly, if you suspect one.
- A handful of tuning and held-out cases is a small sample. Treat a
  recommendation as a candidate for a proper paired study, not a settled result.
- Keep held-out cases for final checks only. Once a held-out result has
  influenced a choice, it's no longer held out.
