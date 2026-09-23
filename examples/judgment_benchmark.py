"""Synthetic comparison of rules, a fake learned predictor and typed services."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from agentloop import (
    AgentTrace,
    JudgeIdentity,
    JudgeUsage,
    JudgmentAnswer,
    JudgmentSpec,
    LocalCallbackJudge,
    judgment_request,
    record_operation,
)
from agentloop.judgment_adapters import LocalPredictorJudge, TypedServiceJudge
from agentloop.judgment_benchmark_types import JudgmentBenchmark, JudgmentExample
from agentloop.judgment_benchmarks import (
    BenchmarkBackend,
    judgment_benchmark_markdown,
    run_judgment_benchmark,
    summarize_judgment_benchmark,
)

FREE = JudgeUsage(0, 0, 0, "reported", "reported")


def examples():
    spec = JudgmentSpec("How likely is the second step redundant?", "probability")
    result = []
    for index in range(25):
        # Independent fixture labels are kept outside the request. These labels
        # and summaries are invented examples, not an empirical workload.
        expected = index % 2 == 1
        label = "similar" if index % 5 == 0 and expected else ("same" if expected else "different")
        if index == 23:
            label = "timeout-fixture"
        trace = AgentTrace(
            "synthetic judgment case",
            run_id=f"judgment-case-{index}",
            started_at="2026-01-01T00:00:00+00:00",
            ended_at="2026-01-01T00:00:00.010000+00:00",
            elapsed_ms=10,
            metadata={"synthetic": True},
        )
        record_operation(
            "fixture step",
            kind="classifier",
            duration_ms=10,
            trace=trace,
            event_id="step",
            started_at=trace.started_at,
            ended_at=trace.ended_at,
        )
        request = judgment_request(trace, spec, ["step"], summaries={"step": label})
        result.append(
            JudgmentExample(
                str(index), request, expected, "independent", f"synthetic-labels:{index}", "1"
            )
            if index < 24
            else JudgmentExample(str(index), request)
        )
    return tuple(result)


def fake_prediction(request, *, timeout_s):
    return {"same": 0.9, "similar": 0.75, "different": 0.15}.get(request.evidence[0].summary, 0.5)


def fake_service(payload, *, timeout_s):
    summary = payload["evidence"][0]["summary"]
    if summary == "timeout-fixture":
        raise TimeoutError
    return {"value": 0.85 if summary in {"same", "similar"} else 0.1, "usage": asdict(FREE)}


def identity(name, *, provider="local"):
    return JudgeIdentity.configured(
        f"example.{name}",
        "1",
        {"fixture_version": "1"},
        provider=provider,
        model_or_rule=name,
        revision="1",
    )


def main(out):
    plan = JudgmentBenchmark(
        "synthetic backend comparison",
        "1",
        examples(),
        repetitions=2,
        seed=7,
        timeout_s=1,
        synthetic=True,
    )
    backends = [
        BenchmarkBackend(
            "rule",
            LocalCallbackJudge(
                identity("rule"),
                lambda item, **options: JudgmentAnswer(
                    float(item.evidence[0].summary == "same"), usage=FREE
                ),
            ),
            "deterministic",
            True,
        ),
        BenchmarkBackend(
            "fake-learned",
            LocalPredictorJudge(identity("fake-learned"), fake_prediction, usage=FREE),
            "local_predictor",
            True,
        ),
        BenchmarkBackend(
            "fake-service",
            TypedServiceJudge(
                identity("fake-service", provider="local-fake"),
                fake_service,
                credentials_required=False,
            ),
            "typed_service",
            True,
        ),
        BenchmarkBackend(
            "optional-service",
            TypedServiceJudge(identity("unconfigured-service", provider="unconfigured")),
            "typed_service",
        ),
    ]
    bundle = run_judgment_benchmark(plan, backends, enabled=True)
    report = summarize_judgment_benchmark(bundle)
    out.mkdir(parents=True, exist_ok=True)
    for filename, value in (
        ("protocol.json", plan.to_dict()),
        ("bundle.json", bundle),
        ("report.json", report),
    ):
        (out / filename).write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")
    (out / "report.md").write_text(judgment_benchmark_markdown(report), encoding="utf-8")
    assert report["planned_invocations"] == 200
    models = {item["name"]: item for item in report["backends"]}
    assert models["fake-service"]["status_counts"]["timeout"] == 2
    assert models["optional-service"]["availability_counts"] == {"not_configured": 50}
    assert all(item["quality"][0]["value"] is None for item in models.values())
    print(f"Wrote synthetic comparison with retained timeout and unlabelled cases to {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/judgment-benchmark"))
    main(parser.parse_args().out)
