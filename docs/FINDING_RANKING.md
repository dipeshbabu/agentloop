# Evidence-aware investigation ranking

Available from an **Unreleased source checkout**. Ranking helps select experiments;
it never applies a finding or changes an estimated saving into a measured result.
Canonical finding IDs, estimator snapshots, predictions and lifecycle decisions
remain intact. Severity is a separate diagnostic field.

Every diagnosis and stored queue item includes a versioned `ranking` object with
components, units, input references, missing/invalid reasons and ordering rules.
Default ordering compares, in sequence: test readiness, declared confidence,
evidence strength, lower quality risk, observed run frequency, cumulative recorded
span work, lower validation effort/cost, estimated latency/cost/token opportunity,
then stable identity. Confidence/evidence tiers are ordinal categories, not
calibrated probabilities. There is no universal ROI formula or dollar/time exchange
rate. A moderate supported opportunity can precede a large speculative estimate.

## Readiness and explicit inputs

`ready_to_test` requires complete span evidence, medium/high rule confidence with
rule provenance, declared/observed evidence or deterministic inference, a known
cost opportunity, some positive modeled opportunity, and complete required inputs.
Inferred/semantic-judge evidence alone remains qualified. High declared quality
risk, irreversibility, invalid inputs or approximate overlap selection require
further evidence/review. `needs_evidence_or_review` retains the finding and all its
original estimates; it does not mean the potential opportunity is zero.

Quality risk, reversibility, validation effort and validation cost have no default
guesses based on finding type. Supply them through trace metadata or the optional
`ranking_inputs` argument to `build_diagnosis`/`rank_findings`:

```python
trace.metadata["agentloop.ranking_inputs"] = {
    "parallelize_tools": {  # Rule ID; a finding-ID entry can override individual inputs.
        "quality_risk": {"value": "medium", "kind": "declared", "source_ref": "criteria:v2"},
        "reversible": {"value": True, "kind": "declared", "source_ref": "rollback:v1"},
        "validation_effort_minutes": {"value": 15, "kind": "declared", "source_ref": "experiment-plan:v1"},
        "validation_cost_usd": {"value": 0.25, "kind": "declared", "source_ref": "experiment-plan:v1"},
    }
}
```

Each input requires an explicit value, evidence kind and nonempty source reference.
These are trusted host declarations for review, not authenticated proof of safe
behavior. Values and references are owned copies. Unknown extra payloads are not
copied into ranking input snapshots. Invalid known inputs remain explicit and
cannot become eligible after persistence.

Optional inputs are `evidence_level` (a declared evidence assessment) and
`estimated_latency_savings_ms`, `estimated_cost_savings_usd`, or
`estimated_token_savings` (all require kind `estimated`). A supplied opportunity
estimate affects ranking only; the original finding prediction remains unchanged
and the alternate source reference is visible. Unmodeled or unknown cost is not
silently treated as a known zero. A zero estimate is accepted only when explicitly
modeled or declared with provenance.

Rule authors can add bounded names to `FindingRule(..., ranking_requirements=(...))`.
The core checks presence and provenance of these scalar inputs without knowing
domain semantics. For example, a rule can require `validation_dataset`; its value
and reference must then be provided before that finding is ready to test. The
standard requirements cannot be removed by a rule.

## Individual dimensions and surfaces

Supported sorts are `priority`, `impact`, `frequency`, `latency`, `cost`, `tokens`,
`confidence`, `evidence`, `quality_risk`, `reversible`, `validation_effort` and
`validation_cost`. Unknown values sort last. Dimension sorting never changes an
item's readiness or grants approval.

```bash
agentloop diagnose --path trace.json --sort-by cost --json-out diagnosis.json
agentloop analyze trace.json --sort-by latency --json-out analysis.json --html analysis.html
agentloop optimization-queue --sort-by validation_effort --json-out queue.json
uv run python -m examples.finding_ranking --out runs/finding-ranking
```

The offline example uses synthetic declarations and performs no optimization.
HTML is sorted at generation time and retains its restrictive content security
policy: source content never becomes executable markup. Its ranking details show
components/references beside the existing estimator provenance. The dashboard
offers the same dimensions and exposes readiness and inputs.

`GET`/`POST /v1/traces/{run_id}/diagnosis` and `GET /v1/optimization-queue` accept
`?sort_by=...`; legacy route aliases retain the same behavior. GET diagnosis remains
read-only. POST preserves human lifecycle decisions while saving the new ranking
metadata. Existing finding-list cursor pagination is unchanged. JSON preserves
both original estimates and ranking inputs/provenance.

## Queue accounting and compatibility

Queues retain compatible-span savings selection per project/run. Overlapping
findings do not add the same opportunity twice. Ranking reuses the existing
selection when its inputs match, recomputing only for different declared estimates.
Approximate selection is reported explicitly. The most cautious member confidence,
evidence and risk determine group readiness; a well-supported member cannot hide
another member's missing inputs. Member records preserve differing declarations.

Observed impact is cumulative duration of distinct recorded spans, which may be
inclusive or concurrent. It is **not elapsed runtime saved**. Frequency counts
included distinct project/run pairs, not a population rate. The stored queue's
existing collection limit still bounds its sample. Equal run IDs in different
projects stay distinct, with `affected_run_refs` identifying their scope.
Overlapping token estimates are retained per member rather than summed without a
joint model.

The compatibility field `priority_score` is now an ordinal among ready
investigations; zero marks items needing evidence/review. Its former weighted
severity/savings formula is retired. `priority_rank` records the default order
even when a different dimension is displayed. Unknown risk remains `unknown`,
`requires_scorer` stays true, and `safe_to_auto_patch` stays false. Ranking provides
no automatic application permission. No storage migration or estimator coefficient
change is introduced.

See [finding rules](FINDING_RULES.md), the [trust benchmark](FINDING_BENCHMARKS.md),
and [reviewed policy promotion](POLICY_PROMOTION.md) for separate detection,
validation and deployment contracts.
