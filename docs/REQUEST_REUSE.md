# Request-local reuse

Agents often call the same read-only tool with the same input several times in
one request: looking up the same product, fetching the same page, or reading
the same file. Reusing the first result saves the repeated calls. Doing that
safely means being strict about what may be reused, and for how long.

`agentloop.request_reuse` reuses results only within a single request, and only
for calls you've declared safe to repeat.

```python
from agentloop.request_reuse import invalidate, request_scope, reusable


@reusable(tool="catalog.lookup", version="2", effect="read")
def lookup(sku: str) -> dict: ...


def handle(request):
    with request_scope(request.id, auth_scope=f"tenant:{request.tenant}/user:{request.user}"):
        lookup("A1")   # runs
        lookup("A1")   # reused
        save_cart(...)
        invalidate("catalog.lookup")  # after a write this request can observe
```

## What may be reused

- **Declared-safe effects only.** `effect` must be `"read"` or `"none"`.
  Decorating with `"write"`, `"external"` or anything else raises `ReuseError`.
  Async functions and generators are refused too.
- **Model output only on request.** Reusing a model call's output needs
  `kind="model"` and `allow_model_output=True`. Identical prompts don't
  guarantee an identical acceptable answer, so this is never the default.
- **Plain inputs only.** Arguments must be plain JSON values: strings, numbers,
  booleans, null, lists, and objects with string keys. Anything else bypasses
  the cache rather than risk two different inputs sharing a key. That includes
  tuples, objects, integer dictionary keys and NaN.

## How long it lasts

The cache exists only inside `request_scope`. When the block ends, it's gone.
Calls outside a scope run normally and store nothing. Scopes can't be nested,
and each thread and each asyncio task has its own. Results therefore never
cross requests, users or tenants.

Within a request, a result is reused only when all of these match:
- the tool and its version;
- the scope's `auth_scope`;
- the `data_version`, if you supply one;
- the canonical input.

On a hit the stored input is compared in full, not just its hash. Pass
`data_version=lambda: store.revision()` to invalidate automatically when the
underlying data changes. Call `invalidate(tool)` or `invalidate()` after a write
the request can see.

Results are deep copied when stored and on every hit, so code that mutates a
result can't change what a later call receives. Results that can't be copied
are returned but not cached. Exceptions are never cached.

## In traces

With a trace active, each call records a tool span whose metadata includes:
- `agentloop.reuse`: whether it was a hit, why, and the key's SHA-256;
- `read_only: true` and `agentloop.effects: {"effect": ...}`;
- `tool_reuse_key`.

The existing parallelism and reuse analysis already reads `read_only` and
`tool_reuse_key`. `scope_stats()` reports hits, misses, bypasses and
invalidations for the current request.

## Context deduplication

`deduplicate_context(segments)` removes later exact copies of context segments
before a model call, for example the same retrieved document twice. A segment
marked `"protected": true`, such as system instructions or policies, is never
removed, even if it repeats. Near-duplicates are kept: one trailing space makes
two segments different.

## Checking that it helped

Record the same workload with and without reuse, check each run's answers with
your own checks, then compare:

```python
from agentloop.request_reuse import compare_reuse

compare_reuse(baseline_trace, candidate_trace, baseline_correct=True, candidate_correct=True)
```

It reports these for each run, and the difference between them:
- tool calls, executed calls and reused calls;
- tokens and runtime;
- cost.

Cost is summed only when every model call has a known cost. Otherwise it stays
`None`, meaning unknown. A candidate whose correctness is `False` is
`rejected_incorrect`, and one whose correctness is unknown is `unverified`.
Neither is ever reported as an improvement. Keep the failed comparisons
alongside the successful ones.
