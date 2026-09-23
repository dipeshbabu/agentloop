# Offline payment-review reference

This synthetic reference profiles a structured review pipeline:

```text
transaction/request fixture
 -> suspicious signal
 -> fixture refund eligibility
 -> manual-review requirement
 -> review route label
```

```console
python examples/payment_review.py --out runs/payment-reference
```

Use a fresh or empty output directory. This is an evaluation workload, not
financial authorization logic. It contains no payment processor, fraud model,
refund policy engine, money movement or production approval service. Real
financial eligibility, fraud, compliance and approval policies remain the
deploying organization's responsibility.

## Frozen inputs and independent outcomes

Eight fixtures contain only qualitative amount bands and fictional evidence/
policy flags. No card, account, bank, payment, transaction or personal identifiers
are included. `within_fixture_window` is an invented fixture signal, not a real
refund time limit or legal rule.

Independent labels cover `suspicious`, `eligible`, `manual_review` and `route`.
Cases include ordinary eligibility, legitimate high-value activity, a small
duplicate signal, missing/conflicting evidence, a failed fixture condition,
required review and missing proof. The application functions receive only the
input record and intermediate state; expected outcomes remain separate.

Missing evidence produces null business decisions and a manual-review label.
That is a deliberately correct abstention under this fixture policy, distinct
from a timeout that fails execution and produces no output at all. Route values
such as `refund_review` and `standard_review` are labels; no refund or denial is
executed.

## Executed configurations

| Configuration | Behavior |
| --- | --- |
| `baseline` | Four deterministic local model-style fixture calls |
| `hybrid` | A fixture call for the suspicious signal, then explicit local rules |
| `cheap` | An amount-only proxy and simplified rules that intentionally miss important evidence/policy constraints |
| `failing` | Fixture calls with a declared eligibility timeout for one case |

The cheap proxy creates a false positive on a legitimate high amount and a false
negative on a small duplicate signal. It also mishandles missing evidence/proof
and required review. Lower fixture cost or agreement with a baseline on some
records cannot establish safety. Independent labelled checks reject its mistakes;
the example never selects or activates a configuration.

## Profiling and evidence

All four decision stages use the [generic workflow contract](WORKFLOW_TRACING.md)
with stage/configuration versions, explicit dependencies, actual local callback
durations, outcomes and input/output references. The workflow and every backend
measurement are synthetic. A failed stage retains its error category and failed
workflow, without fabricating an output or an absent billing response.

The shared local backend uses the documented `json-whitespace-fixture-v1`
tokenizer and a synthetic rate card. Counts are exact for those fixture units,
not real LLM token counts. Reported fees come from the synthetic backend, not an
actual processor or model provider. Span metadata identifies both sources.
These values exercise cost/token accounting; they are not real-world savings.

Every candidate runs on the same frozen inputs and fields-scorer version. Native
quality, replay, paired-study and analysis artifacts retain false positives,
false negatives, labelled abstentions, manual-review outcomes and actual failed
steps. Timings are measured, so normal latency gates can vary for short callbacks;
the reference does not manufacture or claim a general latency improvement.

Outputs include the corpus/summary, per-case quality and replay JSON/Markdown,
native study manifests/reports, and trace/diagnosis/HTML analysis for every run.
Unknown quality and billing remain unavailable. The timeout remains in study
denominators rather than becoming an artificially cheap successful run.

To adapt this reference, supply permission-cleared data, an independently reviewed
organization-owned rubric, explicit implementation/scorer versions and honest
usage provenance. Keep evaluation separate from authorization and action systems.
AgentLoop records and compares the application's decisions; it does not decide
real financial eligibility or grant permission to move money.
