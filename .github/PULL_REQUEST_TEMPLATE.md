## Summary

<!-- What problem does this solve, and why is this approach appropriate? -->

## Related issue

<!-- Use "Closes #123" when applicable. -->

## Validation

<!-- List the exact commands run and any relevant before/after output. -->

```text
uv run --frozen pre-commit run --all-files
uv run --frozen --all-extras python -m pytest -q
```

## Compatibility and risk

<!-- Note public API, trace schema, storage, configuration, dependency, security, or migration effects. -->

## Checklist

- [ ] The change is focused and includes tests for changed behavior.
- [ ] Lint, tests, and relevant AgentLoop replay gates pass.
- [ ] User-facing behavior is documented and the changelog is updated when needed.
- [ ] Shared traces, logs, and screenshots contain no credentials or private data.
- [ ] New dependencies are justified and compatible with Apache-2.0 distribution.
- [ ] Substantial AI assistance is disclosed and the generated work was reviewed.
