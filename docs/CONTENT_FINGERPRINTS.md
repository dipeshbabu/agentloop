# Content fingerprints

Two common kinds of agent waste are invisible in a plain trace:

- A final "check the answer" model call that hands the draft back unchanged.
- A "rephrase the question and retrieve again" step that finds nothing the
  first retrieval didn't.

The [November 2026 optimization benchmark](OPTIMIZATION_BENCHMARK.md) found
both, and no finding flagged them. The [semantic rules](SEMANTIC_WASTE.md) can,
but only with saved judge evidence. Content fingerprints are a cheaper,
deterministic alternative: you record a keyed digest of what a step was given
and what it returned, and two rules compare the digests.

Nothing is recorded unless your code calls `record_content`. Traces without
fingerprints produce exactly the findings they did before.

## Record fingerprints

Call `record_content` inside the span, with the value the step works on and the
value it produced:

```python
from agentloop import record_content, trace_model_call, trace_tool_call

with trace_tool_call("find_sentences"):
    passages = find_sentences(context, query)
    record_content(input=query, output=passages)

with trace_model_call("verify", model="large"):
    checked = llm(f"Check this answer: {draft}")
    record_content(input=draft, output=checked)
```

`input` is the value the step operates on, not necessarily its whole prompt.
For a check it is the draft; for a retrieval it is the query. Values can be
text or JSON-like data: `None`, booleans, numbers, strings, lists, tuples and
dicts with string keys. A list of up to 64 values also records one digest per
item, so a rule can tell that a retrieval returned only passages it had already
seen. Other types raise `TypeError`.

If a model client records its own span, so your code never runs inside it,
pass the span's ID: `record_content(input=..., output=..., span_id=event_id)`.
An integration that records spans itself can merge
`content_metadata(input=..., output=...)` into the span's metadata.

Pass `required=True` for a step that must run whatever it returns, such as a
mandated compliance check. The rules never flag it.

## What is stored

Each fingerprinted span carries this in its metadata:

```json
"agentloop.content": {
  "schema": "agentloop.content/v1",
  "scope": "3f9a0c4e1b2d7a65",
  "input": {"digest": "9d4c…"},
  "output": {"digest": "9d4c…", "items": ["51aa…", "07e2…"]},
  "required": false
}
```

- **No text.** A digest is an HMAC-SHA-256, cut to 128 bits.
- **Not reversible by guessing.** The HMAC key is random, belongs to one trace
  in memory, and is never written anywhere. Someone holding the trace can't hash
  candidate answers to find a match, as they could with a plain hash of a short
  value like `"42"`.
- **What it does reveal: equality within one trace.** A reader can see that two
  recorded values in the same trace were the same. Digests from different traces,
  or from different `scope` values, are never comparable.
- **Conservative normalization.** Text is Unicode NFC, with `\r\n` as `\n`,
  trailing spaces removed from each line, and leading and trailing blank space
  removed. Case and indentation are kept, so `Paris` and `paris` differ, and so
  do two programs that differ only in indentation. Other values are digested as
  canonical JSON (sorted keys), and `"1"` differs from `1`.

A trace loaded from a file gets a new key. Records added after loading get a new
`scope` and are never compared with the earlier ones.

[Trace retention](TRACE_RETENTION.md) in `omit` or `redact` mode replaces span
metadata with summaries, so fingerprints don't survive it. The content rules then
abstain.

## The rules

Both rules compare digests within one trace and one scope. They ignore spans
without a valid record, spans that failed, and spans marked `required`.
Neither can see text, so neither can tell whether an output was correct. That
is why confidence is at most `medium`.

### `unchanged_output` 1.0

A model call whose output digest equals its input digest. Calls are grouped by
span name, and a group is reported when at least half of its fingerprinted calls
were unchanged. The finding lists the unchanged calls, and its observations list
the calls that did change something.

### `repeated_result` 1.0

A tool call, such as a retrieval, with a different input from earlier calls of
the same name, that returned only results those earlier calls had already
returned: the same whole output, or only items already seen. A model call whose
output was that tool call's input (the rephrase that produced the query) is
included when every fingerprinted span that used it was redundant. Calls with an
identical input aren't counted here; that is
[request-local reuse](REQUEST_REUSE.md).

Results are only compared with earlier calls under the **same parent span**.
If one trace holds several tasks, wrap each in its own span, or results that
recur across unrelated tasks would look redundant. A group is reported when at
least half of its eligible calls were redundant.

### Confidence

Confidence is tied to how often the pattern held in the trace:

| Pattern held on | Confidence |
| --- | --- |
| At least 3 spans **and** at least 80% of the eligible spans | `medium` |
| Fewer spans, or 50–80% of them | `low` |
| Under 50% of them | No finding |

A trace with one task and one check therefore gives a `low` finding at most.
Aggregate several traces before acting on one.

### Estimates and validation

The savings estimate is the recorded duration of the affected calls (estimators
`unchanged_output` and `repeated_result`, both uncalibrated). An unchanged output
can still be a check that passed: on another input, it might have caught an
error. Treat each finding as a hypothesis. Accept the change only after a paired
replay on held-out tasks shows no drop in independently scored task quality.

## Evidence

The [finding benchmark](FINDING_BENCHMARKS.md#content-fingerprint-corpus) has a
separate labelled corpus for these rules, with a CI regression gate.
