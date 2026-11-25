# Transformation registry

Patch plans describe what could change. A transformation pass goes one step
further: it produces the exact change as a diff, under rules strict enough that
the change can be tried safely.

```python
from agentloop.transformations import default_registry, execute_candidate, propose

registry = default_registry()
candidate = propose(
    registry,
    "config.json_set",
    {"file": "config/agent.json", "pointer": "/model", "expected": "large-model", "value": "small-model"},
    repo=".",
)
print(candidate["status"], candidate["diff"])
```

`propose` is a dry run: it reads the checkout and writes nothing. Trying the
candidate is a separate, explicit step:

```python
result = execute_candidate(
    candidate,
    repo=".",
    validation=["python", "-m", "pytest", "-q", "tests/test_agent.py"],
    allow_execution=True,
)
result["status"]   # "accepted", "rejected" or "error"
```

## The pass contract

Every pass declares, before it runs, what it applies to and what it needs.
`registry.contracts()` lists them:

| Field | Meaning |
| --- | --- |
| `applies_to` | Finding types, or `host_request`, that the pass can address |
| `required_evidence` | What must hold before it proposes anything |
| `allowed_effects` | The operation effects, from the [execution IR](EXECUTION_IR.md), that it tolerates |
| `objective` | The resource it aims to reduce |
| `validation` | How to check a candidate |
| `rollback` | How to undo it |

A pass either proposes a candidate or abstains with reasons. It never guesses.
Registering a second pass with an existing id is an error, not an override.

## Built-in passes

### `config.json_set`

Change one value in a JSON file inside the checkout, at a JSON pointer such as
`/model/name`. The request names the value you expect to find there, and the
pass abstains if the file says anything else. Type counts too: `3` and `3.0`
are different values. It also abstains for:
- paths outside the checkout, or absolute paths;
- files that aren't `.json`;
- missing pointers;
- a new value equal to the current one.

The file keeps its indentation and line endings.

### `ast.memoize_pure_function`

Add `@functools.cache` to one module-level function, and `import functools` if
the module lacks it. It's deliberately narrow. The request names a traced span,
and the pass proposes only when all of these hold:
- The span has a [source receipt](SOURCE_PROVENANCE.md) that resolves exactly
  against the checkout.
- The span declares `effect: "none"` and `deterministic: true` in
  `agentloop.effects`.
- The function is a plain, synchronous, module-level function that isn't
  already cached. Async functions, generators and methods are refused.

The decorator goes directly above `def`, so tracing decorators still record each
call.

## Candidates and receipts

A candidate records:
- the pass and its version, and the request;
- the evidence it relied on;
- each changed file's path, its digest before and after, and the changed lines;
- a unified diff.

`receipt_sha256` is a digest of all of that. The same request against the same
files gives the same receipt, wherever the checkout lives. Receipts contain no
absolute paths.

`conflicts(candidates)` lists pairs of candidates that change the same or
adjacent lines of a file, or that were proposed against different versions of
it. Apply at most one of each pair.

## Executing a candidate

`execute_candidate` refuses to run unless you pass `allow_execution=True`.
When you do, it:

1. Creates a temporary `git worktree` from `HEAD`, outside your checkout.
2. Proposes the candidate again in that worktree, and rejects it unless the
   receipt is identical. This catches a candidate proposed against uncommitted
   edits. Commit first, or propose again.
3. Writes the change and records `git diff`.
4. Runs your validation command in the worktree with a timeout. It's an argv
   list with no shell. Exit code 0 accepts the candidate. Anything else, or a
   timeout, rejects it.
5. Removes the worktree, then checks that your checkout's `git status` is
   unchanged. `rolled_back` and `original_checkout_unchanged` report both.

Rejected candidates are returned with their exit code and the last part of the
validation output, so failed attempts can be kept as evidence.

## What it won't do

- Edit anything it can't identify exactly, or edit outside the checkout.
- Run without `allow_execution=True`, or run commands through a shell.
- Touch your working tree, commit, push or deploy. Applying an accepted diff is
  your decision.
- Judge whether a change is worth keeping. Validation and replayed quality
  gates do that.

The existing `agentloop patch-plan` command is unchanged.
