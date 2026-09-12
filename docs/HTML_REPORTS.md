# Offline HTML reports

Create one shareable file from a recorded native trace:

```bash
agentloop analyze trace.json --html report.html
```

Open `report.html` in a browser. It contains its own styles, uses no JavaScript or
CDN assets, and needs no network, server, database, or dashboard dependencies.
Expand the evidence sections to inspect estimator inputs and recorded spans.
Finding and parent links jump to the corresponding timeline row.

The report separates recorded runtime/tokens/cost, inferred or declared finding
evidence, and uncalibrated predictions. It shows the longest recorded spans,
compatible-selection metadata, estimator provenance, and trace/research metadata.
Synthetic traces and incomplete analysis are labeled explicitly.

## Include a baseline and quality evidence

The positional trace is the candidate when `--baseline` is supplied:

```bash
agentloop analyze candidate.json --baseline baseline.json \
  --quality-fixtures fixtures.json --min-quality-score 0.9 \
  --html comparison.html --json-out comparison.json
```

The comparison includes observed deltas, configured gate results, indeterminate
costs, and supplied quality evidence. `--json-out` includes the same replay object
in the combined analysis payload. Without a baseline, its original
trace/report/diagnosis/optimization shape remains unchanged.

`analyze` produces an artifact even when comparison gates fail; it is an analysis
command. Use `agentloop replay` or `agentloop ci` when failure should block CI.
Quality comparison options require a baseline. Report generation does not run
candidate application code.

## Content and sharing

Raw event prompts, outputs, and full event metadata are omitted by default.
Trace-level metadata and relevant finding evidence remain included, and supplied
quality fixtures/results may contain raw task data. Review them before sharing.

To include the complete event content intentionally:

```bash
agentloop analyze trace.json --html report-with-content.html --include-content
```

The file then displays a raw-content notice. Values from traces, findings, and
quality results are escaped as text; they cannot create markup, event handlers,
scripts, or external resource references. A restrictive content security policy
blocks scripts and network assets as an additional boundary. Source span IDs
are displayed as text and mapped to generated local anchors.

## CI artifacts and research supplements

After your workflow has produced application traces and installed AgentLoop,
add a report step and upload its single output file:

```yaml
- name: Export analysis report
  run: uv run --frozen agentloop analyze runs/candidate.json --baseline runs/baseline.json --html report.html
- name: Upload analysis report
  uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1
  with:
    name: agentloop-analysis
    path: report.html
    if-no-files-found: error
```

Keep original trace JSON, study manifests, intervention records, and quality
fixtures alongside an HTML research supplement when reproducibility matters.
The HTML is a readable artifact; it does not establish that a suggested change
was implemented or that its predicted savings were measured. See
[CI evidence](CI.md) and [research guidance](RESEARCH.md).
