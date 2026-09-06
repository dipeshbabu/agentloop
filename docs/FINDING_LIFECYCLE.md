# Finding lifecycle and identity

AgentLoop stores diagnosis findings so operators can review recurring optimization work without losing decisions when a trace is analyzed again.

Reading a diagnosis with `GET /traces/{run_id}/diagnosis` (or the deprecated
`/diagnose` alias) does not persist findings or change their lifecycle. Use
`POST /traces/{run_id}/diagnosis`, `AgentLoopClient.save_diagnosis(run_id)`, or
`agentloop remote-diagnose RUN_ID` to explicitly recompute and persist findings.
Trace ingestion also persists initial findings. See the
[production guide](PRODUCTION.md#diagnosis-reads-and-writes) for migration details.

## States

A newly detected finding starts as `detected`.

Supported transitions are:

- `detected` to `accepted` or `dismissed`
- `accepted` to `resolved` or `dismissed`
- `resolved`, `dismissed`, or `superseded` to `detected` when explicitly reopened

`superseded` is assigned by re-diagnosis when a previously active finding no longer appears. Human terminal decisions, `resolved` and `dismissed`, are retained as history instead of being overwritten automatically.

All transitions are scoped by project, run ID, and finding ID. An invalid transition is a conflict; an unknown finding is reported separately.

## Stable finding identity

Finding IDs use a content fingerprint rather than the finding's position in the optimization-card list. The fingerprint includes:

- finding type
- finding title
- sorted affected span IDs

The resulting form is:

```text
al_<finding-type>_<12-hex-digest>
```

This means inserting or reordering an unrelated optimization card does not change an unchanged finding's identity. Re-diagnosing the same evidence therefore preserves its reviewed status.

If the finding itself changes, for example the evidence spans or the step named in its title change, the fingerprint changes. The new finding starts as `detected`; the prior finding follows the normal supersession/history rules. This prevents a `resolved` decision from silently transferring to different evidence.

The `metadata.identity` field is `content-v1` for findings produced by this identity contract.

## Pre-0.6 positional IDs

Older findings used IDs such as `al_route_to_smaller_model_001`, where the suffix was the card's list position. Those IDs are intentionally not reused for new content-fingerprinted findings. On the first re-diagnosis after upgrading, active positional findings that are no longer emitted can become `superseded`; existing `resolved` and `dismissed` rows remain retained as historical decisions.

## Queue behavior

Only active findings contribute to the optimization queue. Resolving or dismissing a finding removes it from active queue counts immediately. Reopening it returns it to active analysis. Re-diagnosis preserves the status of an unchanged content-fingerprinted finding.
