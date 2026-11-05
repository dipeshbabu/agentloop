# Hermes runtime pilot

This pilot runs the actual Hermes plugin manager, `AIAgent.run_conversation`, file
tool, and delegated child agent. An OpenAI-compatible server on loopback returns
deterministic responses and synthetic usage. Each scenario gets a fresh profile,
workspace, session database, and process. It never uses your normal Hermes profile
or a real provider credential.

This is integration evidence. It does not measure live model quality, billing,
production latency, or optimization gains. Recorded timings are diagnostic only.
Auxiliary model calls, gateways, and live provider behavior remain outside the pilot.

## Pinned inputs

| Input | Pin |
| --- | --- |
| Hermes | [v0.22.7, `b200f5bc60e31588ba2cb60e288029c4a54ba5fa`](https://github.com/NousResearch/hermes-agent/tree/b200f5bc60e31588ba2cb60e288029c4a54ba5fa) |
| Host dependencies | Upstream `uv.lock`, installed frozen without development extras |
| Host Python | Stable CPython 3.14; exact patch recorded in each report |
| Hermes installer | uv 0.13.7, separate from AgentLoop's uv 0.11.28 |
| AgentLoop observer | Exported from this source checkout; recorder SHA-256 in each report |

The runner checks the Hermes revision and tracked source before and after each
case. It records the upstream lockfile digest. Generated evidence is marked
`synthetic: true`; qualification adds provenance metadata without changing events,
usage, outcomes, timestamps, or durations. The validator checks raw and qualified
artifact hashes and compares their contents.

## Scenarios and expected evidence

| Scenario | Evidence checked |
| --- | --- |
| Baseline | Same user input and final response with observer disabled; no trace |
| Streaming | Real SSE consumption, one model span, synthetic usage of 17 input / 5 output tokens |
| File tool | Actual `read_file` dispatch and successful fixture content; two model attempts |
| Retry | HTTP 429, failed attempt with unavailable usage, retry event, successful attempt |
| Interruption | Real stream callback and `interrupt(hard_cancel=True)` from another thread; unfinished request and incomplete capture; replay/CI fail |
| Tool failure | Missing file retains tool error even though the host turn completes; task quality fails |
| Default privacy | `capture_content` omitted; no model, tool, or final output retained |
| Delegation | Actual child agent reads the file; linked parent/child traces, each with two model calls and 34 / 10 tokens; child usage stays separate |

All model responses use the fake model `agentloop-local-pilot`. Its counts are
synthetic provider data, not observed real-model tokenization. Its price remains
unknown. Completed host execution and task correctness are evaluated separately;
privacy-disabled output cannot serve as task-quality evidence.

## Reproduce on Linux or WSL

Use a current AgentLoop source checkout, Python 3.14, Git, and both pinned uv
versions as separate executables. Replace the executable paths below with your
local installations. Run these commands from the AgentLoop checkout:

```bash
/path/to/uv-0.11.28 sync --locked --no-dev --python 3.14
pilot=$(mktemp -d)
.venv/bin/agentloop hermes export-plugin "$pilot/plugin"
git clone https://github.com/NousResearch/hermes-agent.git "$pilot/hermes"
git -C "$pilot/hermes" checkout --detach b200f5bc60e31588ba2cb60e288029c4a54ba5fa
(cd "$pilot/hermes" && /path/to/uv-0.13.7 sync --frozen --no-dev --python 3.14)
for scenario in baseline stream tool retry interrupt tool-error privacy delegation; do
  timeout --signal=TERM --kill-after=10s 180s \
    "$pilot/hermes/.venv/bin/python" -I scripts/smoke_hermes_runtime.py \
    --plugin-dir "$pilot/plugin" --hermes-root "$pilot/hermes" \
    --out "$pilot/evidence/$scenario" --scenario "$scenario"
done
.venv/bin/python scripts/validate_hermes_runtime.py --root "$pilot/evidence"
```

The harness clears inherited provider/proxy settings before importing Hermes and
limits Python socket clients to the owned loopback provider port. This guard is
not an operating-system sandbox. Only file/delegation toolsets are enabled, using
the owned fixture. Dependency installation requires network access before runtime.

`validation.json` summarizes all eight cases. Each scenario also has a report,
raw native traces under `profile/agentloop/traces`, qualified copies under `native`,
and HTML generated through AgentLoop's analysis pipeline. The baseline has no trace.
The [CI workflow](../.github/workflows/ci.yml) runs the same cases and uploads these
artifacts, excluding profile databases, caches, and installed dependencies.

This compatibility extension and incomplete-capture replay fix are **unreleased**.
The immutable PyPI 0.1.1 release accepts Hermes `>=0.21.6,<0.22`; use current source
for this pilot and Hermes 0.22.x. See [the integration guide](HERMES.md).
