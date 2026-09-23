# Offline incident-triage reference

This reference profiles an AI-assisted decision pipeline using synthetic signals:

```text
signals -> anomaly -> severity -> response proposal -> rollback/escalation proposal
```

```console
python examples/incident_triage.py --out runs/incident-reference
```

Use a fresh or empty output directory. The example never loads cloud credentials,
executes a command, changes infrastructure or performs a rollback. Suggested
responses are inert structured values: `execute` and `execution_permitted` are
always false. This is not an incident platform, executor, rollback engine or
security sandbox.

## Frozen signals and policy comparisons

Seven independent fixture outcomes cover normal operation, noisy signals,
ambiguous evidence, severe incidents with and without a documented rollback
candidate, a warning, and recovery. Inputs are qualitative synthetic flags rather
than production hosts, cloud identifiers, secrets or operational runbooks.

| Configuration | Purpose |
| --- | --- |
| `baseline` | Local model-style fixtures, repeated severity analysis and irrelevant context |
| `hybrid` | A fixture-backed anomaly stage plus rules, preserving required verification |
| `cheap` | A latency-only shortcut that misses sustained-error incidents and over-escalates noise/warnings |
| `failing` | A declared timeout while producing a response proposal for one severe case |

Missing evidence yields an unknown anomaly/severity and a gather-evidence/escalation
proposal. That is a labelled abstention, distinct from a failed execution with no
output. A documented rollback candidate is a fixture signal, not proof that a
rollback is operationally safe. Real response policies, authorization, safety
checks and infrastructure execution remain outside this reference.

## Observations and evidence

Detection, severity, response and disposition stages use generic workflow/stage
metadata with declared dependencies, implementation versions, actual callback
timing, outcomes and opaque input/output references. Required secondary checks
remain distinct stages. Expected outcomes are not supplied to the decision
functions; independent fields-scorer version 1.0 checks the full structured output.

Model-style calls use the shared deterministic fixture backend and its explicitly
synthetic fee/rate-card provenance. `json-whitespace-fixture-v1` counts fixture
token units, not real LLM tokens. All traces and reports are marked synthetic.
Recorded local timing and normal replay gates remain visible, but short callback
noise is not presented as a stable real-model performance improvement.

Replay and paired studies retain missed incidents, over-escalation, unknown
business states, failed steps and unavailable billing/quality. Lower fixture cost
does not override the labelled quality checks. The output includes the frozen
corpus, comparison inventory, native replay/quality/study reports and per-run
trace, diagnosis and standalone HTML analysis.

## Optional semantic investigation

Baseline traces investigate repeated severity analysis and irrelevant context
through the typed judgment contract. A deterministic local fixture judge reads
approved summaries by default; `main(out, judge=my_backend)` accepts another
explicit backend, with any external spend/cancellation owned by that host.

Required verification is marked for retention and cannot become a redundancy
recommendation. Other supported cases remain low-confidence hypotheses with
unavailable removal savings unless separately attributable. These findings say
nothing about whether a response or rollback is safe to execute. Task quality is
checked independently against frozen fixture outcomes.

AgentLoop observes and evaluates the application-owned triage policy. Adapting
the example to permission-cleared incident data requires organization-owned
expectations, implementation/scorer versions and honest provider usage; it does
not turn profiling evidence into production authorization.
