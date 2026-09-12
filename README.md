# AgentLoop

[![CI](https://github.com/dipeshbabu/agentloop/actions/workflows/ci.yml/badge.svg)](https://github.com/dipeshbabu/agentloop/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/agentloop-profiler.svg?cacheSeconds=300)](https://pypi.org/project/agentloop-profiler/)
[![Downloads/month](https://static.pepy.tech/badge/agentloop-profiler/month)](https://pepy.tech/projects/agentloop-profiler)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB.svg)](https://www.python.org/downloads/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

**Profile your AI agent and check whether a change improves it.**

AgentLoop records model calls, tool calls, and retries in an agent workflow. Use
its reports to inspect latency, token usage, and estimated model costs, find
repeated work, and compare runs before and after a change.

Tracing and analysis run locally. You can work with JSON files without a database
or hosted account. The dashboard and HTTP API are optional.

## Quickstart

Requires Python 3.10 or newer:

```bash
python -m pip install agentloop-profiler
agentloop quickstart
```

The package name is `agentloop-profiler`; the Python import and CLI command are
both `agentloop`.

`quickstart` creates `runs/agentloop_quickstart.json` and prints findings from
synthetic data. Use it to check the installation and learn the report format.
The demo itself makes no network or model API calls.

Inspect the saved trace and export the complete analysis:

```bash
agentloop analyze runs/agentloop_quickstart.json --json-out runs/quickstart_analysis.json
```

The analysis JSON contains the trace, its metrics, findings, and optimization
suggestions. See [First useful result](docs/FIRST_USE.md) for more options.

## Record your own run

Save this complete example as `example_agent.py`. It traces a few local function
calls so you can try instrumentation without a model SDK or API key. Replace the
function body with your own work when adapting it to an agent.

```python
import agentloop

NOTES = {
    "tracing": "Record the steps in a workflow.",
    "optimization": "Investigate repeated or expensive work.",
    "replay": "Compare saved baseline and candidate traces.",
}


@agentloop.trace_tool(name="lookup_note")
def lookup_note(topic: str) -> str:
    return NOTES[topic]


with agentloop.trace_agent("research_agent") as trace:
    output = {"notes": [lookup_note(topic) for topic in NOTES]}
    trace.metadata["output"] = output

trace.export_json("runs/research_agent.json")
print(output)
```

Run it, then analyze the file it created:

```bash
python example_agent.py
agentloop analyze runs/research_agent.json --json-out runs/research_agent_analysis.json
```

The decorator records each function call's duration and status. The example also
explicitly saves the final output in trace metadata so the quality checks below
can read it. Decide which outputs are appropriate to retain when adapting this
example to real data.

For model calls, use an [SDK integration](#framework-integrations) to capture
provider token usage. The [integration guide](docs/INTEGRATIONS.md) also covers
model decorators, context managers, streaming, and cancellation.

## Inspect findings

Start with `analyze`. It combines metrics, findings, and proposed optimizations
in one report. AgentLoop looks for patterns such as repeated context, retry loops,
repeated model calls, large steps, and recurring tool calls.

On `main`, reports and optimization use the same [versioned finding rules](docs/FINDING_RULES.md).
If a rule fails, its diagnostics remain visible and analysis is marked incomplete.
CI requires complete analysis before recommending a merge.

Treat a suggestion as a change to investigate. For example, repeated tool names
alone do not establish that calls are safe to run concurrently. The development
version on `main` exposes the evidence and assumptions behind
[parallelization candidates](docs/PARALLELIZATION.md).

Use individual commands when you need a specific artifact:

| Command | Output |
|---|---|
| `agentloop report runs/research_agent.json` | Metrics for one run. |
| `agentloop diagnose --path runs/research_agent.json` | Findings with affected spans and validation criteria. |
| `agentloop optimize --path runs/research_agent.json` | Suggested changes and estimated savings. |
| `agentloop patch --path runs/research_agent.json --repo .` | Proposed edits for the repository at `.`, ready for you to review and apply. |

Change one thing at a time, such as reducing repeated context or changing a retry
policy, so you can attribute the result to that change.

## Compare before and after

Run your agent before the change and export `runs/baseline.json`. Run the changed
agent on the same task and export `runs/candidate.json`. `replay` compares these
saved traces:

```bash
agentloop replay --baseline runs/baseline.json --candidate runs/candidate.json --min-latency-improvement-pct 10
```

This requires at least a 10% latency improvement. The report also compares costs,
tokens, retries, and call counts. It writes `runs/replay_report.md` and exits
nonzero when a required gate fails. Use `agentloop replay --help` to choose other
thresholds or a JSON output path.

A result applies to the runs and checks you supplied. Repeat the comparison across
representative tasks before drawing broader conclusions. Add output checks when
you need evidence that the changed agent still meets its task requirements.

## Quality gates

Quality fixtures define checks against the recorded output. For the example
above, save this as `fixtures.json`:

```json
{
  "fixtures": [
    {
      "id": "notes_present",
      "scorer": {"type": "required_fields", "required": ["notes"]}
    }
  ]
}
```

Run the check against the example trace:

```bash
agentloop quality-report fixtures.json --candidate runs/research_agent.json
```

This checks that the final output has a nonempty `notes` field. Choose expectations
that express correctness for your own task. Fixtures can provide outputs directly
or use a trace's `metadata.output`; the scorer can also use the last model call's
recorded output text when available.

| Scorer | Check |
|---|---|
| `exact_match` | Output exactly matches an expected value, including its type. |
| `contains` | Output contains specified text. |
| `glob` | Output matches a bounded wildcard pattern. |
| `required_fields` | A JSON object contains the specified nonempty fields. |
| `json_subset` | A JSON object contains specified top-level keys with matching values. |
| `custom` | A trusted local Python function, configured as `module:function`, scores the output. |

Every candidate case must pass. If you set `--min-quality-score` in `replay`/`ci`
(or `--min-score` in `quality-report`), the average score must also meet that threshold.
Invalid suites fail closed.

Custom scorers run Python code and should be used only with trusted local fixtures.
The HTTP endpoint rejects them. See the [changelog](CHANGELOG.md) for `regex` and
`json_schema` migration notes.

Once you have baseline and candidate traces, use the same fixtures in CI:

```bash
agentloop ci --baseline runs/baseline.json --candidate runs/candidate.json --quality-fixtures fixtures.json
```

`ci` writes a Markdown report suitable for a pull request and exits nonzero when
a required gate fails. It evaluates the traces your application supplies.

The repository's required replay check uses synthetic demos to test AgentLoop's
CI machinery. For comparisons using your application's artifacts, see
[CI self-tests and application comparisons](docs/CI.md).

## Understand the results

These notes describe the current `main` branch. Check [Unreleased changes](CHANGELOG.md#unreleased)
for behavior that has not yet shipped in the published package.

| Result | How to interpret it |
|---|---|
| Runtime and call counts | Recorded execution data. `total_runtime_ms` is elapsed time; `cumulative_span_time_ms` sums spans and can be larger when calls overlap or nest. |
| Operation kinds | Agent, workflow, model, tool, retrieval, and other roles recorded alongside legacy event categories. See [Operation kinds](docs/OPERATIONS.md). |
| Token counts | Counts identify their source: a provider, a tokenizer, user code, or a word-count estimate. Missing usage is marked unavailable. |
| Model cost | A calculation from token counts and configured pricing. Unknown prices or estimated usage limit the comparisons you can make. |
| Optimization savings | Uncalibrated predictions with versioned formulas, coefficients, and assumptions. Measure a candidate run to establish the actual effect. |
| Quality scores | Results of the checks you supplied. Their coverage depends on your fixtures. |

Cost gates can be marked `indeterminate` when pricing or usable token counts are
missing. An indeterminate cost comparison does not fail the default gate; requiring
a positive `--min-cost-improvement-pct` makes unverifiable cost improvement fail.
See [Pricing](docs/PRICING.md) and [Trace compatibility](docs/TRACE_SCHEMA.md) for
the details, including how older traces are handled.

The optimizer avoids double-counting suggestions that affect the same spans.
Reports say whether the chosen combination is exact or approximate. See
[Savings selection](docs/SAVINGS_SELECTION.md).
For individual predictions, see [Estimator provenance](docs/ESTIMATORS.md).
The [intervention ledger](docs/INTERVENTIONS.md) links finding predictions to
measured candidate results and preserves quality checks in an exportable record.
For experiments across tasks and repetitions, use [paired study summaries](docs/STUDIES.md).

Keep your Markdown renderer's safe mode or HTML sanitizer enabled when viewing
reports from untrusted traces.

## Framework integrations

| Integration | How it connects |
|---|---|
| Custom Python agents | Decorators and context managers around your functions. |
| OpenAI SDK | Wrap a client or callable to capture calls, streams, and reported usage. |
| OpenAI Agents SDK | Attach an AgentLoop tracing processor. |
| LangGraph | Instrument the builder before adding nodes, then wrap the compiled runnable. |
| CrewAI | Wrap crew, task, or agent execution methods. |
| OpenTelemetry / Vercel AI SDK telemetry | Import supported telemetry into AgentLoop traces. |

The adapters are included in AgentLoop. Install the framework's own SDK separately
when needed; `python -m pip install "agentloop-profiler[instrumentation]"` includes
the OpenAI Python SDK. See [Framework integrations](docs/INTEGRATIONS.md) for
setup and supported behavior.

## Dashboard and persistence

The dashboard reads stored traces. From a [source checkout](#source-checkout),
load the synthetic example and start Streamlit:

```bash
uv run --frozen --all-extras agentloop quickstart
uv run --frozen --all-extras agentloop store-trace --path runs/agentloop_quickstart.json --project-id default
uv run --frozen --all-extras streamlit run dashboard/app.py
```

Open the URL printed by Streamlit and leave the sidebar's Project field set to
`default`. To inspect your own data, store your trace file with `store-trace`.

AgentLoop uses SQLite by default and supports Postgres for shared storage. The
optional HTTP API accepts traces and serves project-scoped reports and findings.
See [Dashboard setup](docs/DASHBOARD.md) and [Production deployment](docs/PRODUCTION.md)
for database configuration, API authentication, and deployment instructions.

## Other installation options

### Source checkout

Use a source checkout for the dashboard or the latest unreleased changes.
Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run:

```bash
git clone https://github.com/dipeshbabu/agentloop.git
cd agentloop
uv sync --locked --all-extras
uv run --frozen --all-extras agentloop quickstart
```

### Standalone CLI

The [GitHub Releases page](https://github.com/dipeshbabu/agentloop/releases) has
executables for Linux x86-64, Windows x86-64, macOS Intel, and macOS Apple silicon.
They bundle Python and the core CLI. Use the Python package for SDK instrumentation,
the dashboard, API server, or Postgres support.

Use the downloaded executable wherever the examples show `agentloop`.

<details>
<summary>Download and verify a standalone executable</summary>

Download the matching file and `SHA256SUMS` from the same release. Replace `X.Y.Z`
with its version number.

Linux (Bash):

```bash
set -euo pipefail
version=X.Y.Z
asset="agentloop-v${version}-linux-x86_64"
curl -LO "https://github.com/dipeshbabu/agentloop/releases/download/v${version}/${asset}"
curl -LO "https://github.com/dipeshbabu/agentloop/releases/download/v${version}/SHA256SUMS"
mapfile -t checksum_lines < <(awk -v asset="$asset" '$2 == asset { print }' SHA256SUMS)
if [ "${#checksum_lines[@]}" -ne 1 ]; then
  echo "Expected exactly one checksum for ${asset}" >&2
  exit 1
fi
printf '%s\n' "${checksum_lines[0]}" | sha256sum --check --strict
chmod +x "$asset"
./"$asset" --help
```

Windows PowerShell:

```powershell
$Version = "X.Y.Z"
$Asset = "agentloop-v$Version-windows-x86_64.exe"
Invoke-WebRequest "https://github.com/dipeshbabu/agentloop/releases/download/v$Version/$Asset" -OutFile agentloop.exe
Invoke-WebRequest "https://github.com/dipeshbabu/agentloop/releases/download/v$Version/SHA256SUMS" -OutFile SHA256SUMS
$ChecksumLines = @(Get-Content SHA256SUMS | Where-Object {
    $_ -match "^[0-9a-fA-F]{64}  $([regex]::Escape($Asset))$"
})
if ($ChecksumLines.Count -ne 1) { throw "Expected exactly one checksum for $Asset" }
$ExpectedHash = ($ChecksumLines[0] -split "  ", 2)[0]
$ActualHash = (Get-FileHash .\agentloop.exe -Algorithm SHA256).Hash
if ($ActualHash -ne $ExpectedHash) { throw "Checksum verification failed for $Asset" }
.\agentloop.exe --help
```

Each release includes platform-specific third-party notices. macOS executables
are ad-hoc signed but not notarized; Gatekeeper may require approval on first launch.

</details>

## Research use

For experiments, record one trace per task attempt and condition. Attach metadata
such as task ID, seed, model, and source commit, and repeat comparisons across your
task set. The [research guide](docs/RESEARCH.md) explains this workflow. From a
source checkout, run the offline paired example with:

```bash
uv run --frozen --all-extras python examples/research_experiment_demo.py
```

## Help and contributions

Run `agentloop --help` for all commands, or `agentloop COMMAND --help` for a specific
command. Use `agentloop doctor` to inspect your installation and available integrations.

- [Support](SUPPORT.md): usage help and troubleshooting.
- [Issue forms](https://github.com/dipeshbabu/agentloop/issues/new/choose): bugs and feature requests.
- [Contributing](CONTRIBUTING.md): development setup and required checks.
- [Roadmap](docs/ROADMAP.md) and [changelog](CHANGELOG.md): project direction and release changes.
- [Security](SECURITY.md): private vulnerability reports.
- [Governance](GOVERNANCE.md) and [Code of Conduct](CODE_OF_CONDUCT.md): project decisions and participation.

AgentLoop is under active pre-1.0 development. Use a tagged release when you need
a fixed version, and check the changelog before upgrading. Maintainers can use the
[launch checklist](docs/OPEN_SOURCE_CHECKLIST.md) when preparing a public release.

## License

Copyright 2026 Dipesh Tharu Mahato.

AgentLoop is licensed under the [Apache License 2.0](LICENSE). Dependencies retain
their own terms; see [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
