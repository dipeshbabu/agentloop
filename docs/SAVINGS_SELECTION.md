# Savings selection accuracy

AgentLoop optimization cards can overlap. Two cards that touch the same execution span are alternatives because applying one can change or remove work targeted by the other. AgentLoop therefore reports savings from one span-disjoint subset instead of summing every card.

## Objective

The selector optimizes lexicographically:

1. maximize estimated latency savings;
2. when latency ties, maximize estimated cost savings;
3. when both tie, choose the lexicographically smallest card-index set so repeated runs are deterministic.

Latency and cost always come from the same compatible subset.

## Exact and approximate paths

Overlap-connected components with at most 24 items use exact branch-and-bound search. The result is marked:

```json
{
  "selection_optimal": true,
  "selection_algorithm": "exact_branch_and_bound",
  "exact_component_limit": 24
}
```

A component above that limit uses a deterministic greedy fallback so analysis remains bounded on unusually large or dense plans. The result is then marked:

```json
{
  "selection_optimal": false,
  "selection_algorithm": "hybrid_exact_greedy",
  "exact_component_limit": 24
}
```

An approximate result remains a valid compatible plan, but it is not a proof of the maximum achievable savings and can understate what a better compatible selection could save. AgentLoop does not present that path as optimal.

The machine-readable fields live under `savings_aggregation` in optimization-plan output. Queue prioritization uses the same `select_compatible()` selection routine for per-run savings, so it does not switch to a different double-counting rule.

## Why there is a cutoff

Exact compatible selection is a maximum-weight independent-set problem on the overlap graph and has exponential worst-case complexity. The cutoff keeps common plans exact while preventing one pathological component from making profiling unbounded. If real workloads routinely cross the cutoff, the preferred next step is a stronger bounded solver with a documented guarantee rather than silently increasing the limit indefinitely.
