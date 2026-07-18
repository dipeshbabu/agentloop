# Issue #12 — Fail closed when quality fixtures are empty, invalid, or failing

- **Priority:** P1 · **Effort:** M · **Labels:** bug, python, code-quality
- **Link:** https://github.com/dipeshbabu/agentloop/issues/12

## Problem

Two fail-**open** paths: (1) an empty fixture list produces a *passing* report; (2) a failing
quality report does not fail replay/CI unless `min_quality_score` is also set. Scorer/fixture
shapes are permissive enough that missing expectations pass vacuously (e.g. `contains` with an
empty expected string always passes).

## Reproduction (current main)

```json
{ "failed_fixture_report_passed": false, "replay_passed": true, "empty_suite_passed": true }
```

## Key files

- `agentloop/quality.py:49` — success initialized as `not failed_cases`, so zero cases pass.
- `agentloop/replay.py:316-332` — `quality_report["passed"]` only consulted inside the optional min-score gate.
- `load_quality_fixtures()` — returns `[]` for an object without `fixtures`.
- Scorers are untyped dicts.

## Approach

1. **Validate fixture & scorer schemas before scoring.** Reject missing `fixtures`, empty
   requirements, and malformed scorer configs with actionable errors.
2. **Reject zero-case suites** unless an explicit, named non-gating/report-only mode is
   requested.
3. **Make a failing supplied fixture fail replay/CI by default** — independent of any numeric
   `min_quality_score` threshold.
4. **Range-check** `min_score` and custom scorer scores to a documented range (e.g. [0,1]).
5. **Consistent, actionable validation errors** across CLI, API, and dashboard.
6. If a report-only mode is kept, make it explicit in API/CLI names/options.

## Acceptance criteria (from the issue)

- [ ] Empty suites fail validation with a clear message.
- [ ] Missing/empty scorer requirements cannot pass vacuously.
- [ ] Any failing supplied fixture makes replay and CI fail by default.
- [ ] `min_score` and custom scorer scores are range-checked.
- [ ] A deliberate report-only mode, if retained, is explicit in API and CLI names/options.
- [ ] Regression tests cover CLI exit codes, report JSON, the performance workflow path, HTTP input, and dashboard parsing.

## Testing

- `tests/test_quality.py` / `test_replay.py` / `test_ci_command.py`: empty suite → fail;
  vacuous scorer → fail; failing fixture → replay & CI non-zero exit without min-score set;
  out-of-range `min_score` → error.

## Compatibility / risk

- This **flips** currently-green pipelines that relied on empty/failing suites passing —
  document prominently in the changelog. Verify `agentloop-performance.yml` still behaves.
