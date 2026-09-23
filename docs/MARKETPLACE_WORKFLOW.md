# Marketplace/content reference

This reference profiles a synthetic listing pipeline:

```text
listing -> duplicate -> fixture policy category -> product category -> review route
```

```console
python examples/marketplace_workflow.py --out runs/marketplace-reference
```

Use a fresh or empty output directory. It is not a marketplace product, ranking
system, universal moderation taxonomy or automatic moderation service. No listing
is published, removed, ranked or changed; all routes are inert review labels.

## Frozen cases and implementations

Eight fixtures cover a new listing, exact duplicate, similar title with a distinct
variant, benign keyword, explicit fictional policy flag, ambiguous category,
missing evidence and required verification. Identity keys and content are synthetic.
The `policy_flag` is a fixture annotation, not a real platform policy judgment.

| Configuration | Behavior |
| --- | --- |
| `baseline` | Local model-style fixtures, including an overlapping identity recheck |
| `hybrid` | Exact identity/routing/category rules plus a moderation fixture, retaining required verification |
| `cheap` | Title-only duplicate matching, keyword moderation and a guessed category |
| `failing` | A declared category-step timeout for one listing |

The cheap candidate mistakes a near-duplicate variant for an exact duplicate. It
flags a benign title containing "flag" and misses a fixture policy flag without
that keyword. Unknown evidence/category is correctly deferred to manual review
by the baseline and hybrid; the cheap candidate's guesses fail independent labels.
Lower fixture cost is reported separately and does not establish acceptable quality.

The expected duplicate, moderation, category, route and verification fields are
stored independently of the decision functions. All variants use the same frozen
inputs, corpus hash, configuration version and fields-scorer version.

## Stage quality and final routing

Each run records generic workflow/stage identity, explicit dependencies, actual
callback durations, outcome and input/output references. Model-style calls and
rule stages remain distinguishable. The shared synthetic backend names its fixture
tokenizer and fee rate card; these are not real-model token usage or actual billing.

The usual `quality.json` remains the strict full-output quality assessment used
by replay and studies. Separate `field-quality.json` and `field-quality.md` files
contain one diagnostic case per output field. A wrong duplicate or moderation
decision and the resulting wrong route remain individually visible instead of
being hidden behind aggregate success. These diagnostic cases do not change the
weighting of the primary quality score. Execution failures retain their failed
stage and unavailable final output/quality in the original trace and reports.

Native replay keeps measured latency and normal gates; short local timing noise
does not justify a general speed claim. The reference asserts deterministic labels,
fixture cost reductions and retained errors, not a guaranteed latency improvement.

## Semantic overlap

An explicit typed investigation compares the baseline's duplicate stage and
identity recheck using approved summaries. The default judge is a deterministic
local fixture; `main(out, judge=my_backend)` supports another explicitly configured
backend. Host-owned external usage requires its own budget/cancellation controls.

Required verification is marked for retention. Other supported overlap findings
remain low-confidence hypotheses, with no unsupported removal savings. They do
not establish moderation correctness or authorize changes to a listing. Independent
output labels provide the quality check, not agreement with the semantic judge.

## Artifacts and boundaries

The output contains frozen fixtures, a comparison inventory, full and per-field
quality reports, replay JSON/Markdown, native paired studies and per-run trace,
diagnosis and standalone HTML analysis. Missing/failed outputs remain in study
denominators. No candidate is automatically selected or activated.

Adapting this example requires permission-cleared items, organization-owned policy
criteria, independent labels/checks and truthful implementation/usage provenance.
AgentLoop profiles and evaluates the application's decisions; it does not provide
a universal prohibited-content policy or a production moderation authority.
