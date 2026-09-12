# CI self-tests and application comparisons

The repository's **AgentLoop replay self-test** generates synthetic demo traces
and verifies replay/quality gating and report generation. Passing it confirms the
machinery works on those fixtures. It does not demonstrate that an arbitrary PR
improves AgentLoop or an application's performance. The existing required-check
name, `Replay and optimization gates`, is retained for branch-policy compatibility.

CI JSON and Markdown identify each input's run ID, path when available, producer
source, and synthetic marker. A missing marker is reported as missing rather than
as proof that the data came from production. Comparisons containing synthetic
data are explicitly qualified. Demo generators continue to mark their traces.

## Compare application traces

Run your application's baseline and candidate, then upload the resulting trace
files as an artifact in the same workflow run. The reusable workflow downloads
that artifact and runs the configured gates. It never generates replacement demos:
missing artifacts, missing trace files, malformed suites, and failed gates fail
the job. Reports remain available as artifacts and in the job summary.

```yaml
jobs:
  record:
    runs-on: ubuntu-latest
    steps:
      # Check out/install your application and run its baseline/candidate here.
      # Produce baseline.json, candidate.json, and optional quality.json in traces/.
      - uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a
        with:
          name: application-traces
          path: traces/
          if-no-files-found: error
  compare:
    needs: record
    uses: dipeshbabu/agentloop/.github/workflows/application-performance.yml@main
    permissions:
      contents: read
    with:
      trace_artifact: application-traces
      baseline_trace: baseline.json
      candidate_trace: candidate.json
      quality_fixtures: quality.json
      min_latency_improvement_pct: 10
      min_cost_improvement_pct: 5
      min_quality_score: "0.9"
      agentloop_ref: main
```

Replace the comments with your application's setup and tracing commands. Omit
`quality_fixtures` and `min_quality_score` when no quality suite is provided.
All candidate cases must pass when a suite is supplied. Threshold defaults for
latency/cost improvement are zero; an unevaluable positive cost target fails.
Use distinct `report_artifact` names when comparing multiple pairs in one run.

For repeatable CI, pin both the reusable workflow reference and `agentloop_ref`
to the same reviewed AgentLoop commit. The workflow checks out AgentLoop in its
own directory and installs its locked core dependencies. It does not install your
application or SDKs. Use built-in quality scorers here; custom Python scorers that
need application modules belong in your application's own job using `agentloop ci`.

The repository smoke workflow exercises artifact upload/download, the reusable
workflow, and quality gates using explicitly synthetic fixtures. These smoke
results carry the same synthetic-data qualification.

## Manual repository runs

The existing `agentloop-performance.yml` workflow also accepts checked-in trace
paths through `workflow_dispatch`. Set `generate_demo_traces` to the string
`false` to use those files without generating demos. They must exist at the
selected revision; failed file checks never fall back to demo data.
