# Offline onboarding measurement

The [raw benchmark](benchmark.json) records 20 measured rounds of 100 calls per
condition after two warm-up rounds, on September 30, 2026. Conditions rotate in
order. Measurements use the maintained OpenAI-shaped local response fixture and
the version-pinned synthetic OpenInference telemetry fixture. No model provider
or remote service is called.

| Condition | Median | Observed p95 |
| --- | ---: | ---: |
| Unchanged local callback | 0.116 microseconds/call | 0.351 microseconds/call |
| Installed adapter, no active trace | 2.472 microseconds/call | 7.443 microseconds/call |
| Active recording, including host boundary | 11.709 microseconds/call | 19.598 microseconds/call |
| Paired active-minus-baseline increment | 11.577 microseconds/call | 19.480 microseconds/call |
| Parsed OpenInference payload import | 0.289 milliseconds/import | 0.401 milliseconds/import |

These are descriptive local timings, not a production latency or overhead
guarantee. The baseline callback does almost no work; its relative slowdown would
not model a provider-backed application. Import timing excludes JSON parsing,
file IO and exporter work. The conformance suite was also running locally during
this measurement; raw slower repetitions remain in the artifact. The result is
not a controlled comparison across machines or a confidence interval.

Setup definitions count three SDK steps (install, initialize the explicit adapter,
add a host boundary) and two existing-telemetry steps (export, onboard). Exporter
configuration, credentials for an actual provider, and independent task-quality
validation are outside those counts. The examples preserve business calls; there
is no claim of universal zero-code instrumentation.

Reproduce from the source checkout with a new output path:

```bash
uv run --frozen python examples/onboarding_benchmark.py --out runs/onboarding-repeat.json
```

The benchmark refuses to overwrite prior evidence. Runtime defaults are reset
in its standalone entrypoint so auto-upload/store settings cannot introduce
external work. The artifact records Python/platform, fixture byte hash, source
hashes with UTF-8/newlines normalized to LF, raw condition timings and summaries.
Source hashes identify the measured code independently of checkout line endings.
No timing threshold runs in CI; behavioral correctness uses the shared integration
and telemetry conformance suites.
