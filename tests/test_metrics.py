from __future__ import annotations

from agentloop.tracer import trace_agent, trace_model_call, trace_tool_call


def test_report_has_basic_metrics() -> None:
    with trace_agent("test") as trace:
        with trace_model_call("m", model="gpt-4.1-mini", input_tokens=100, output_tokens=20):
            pass
        with trace_tool_call("t"):
            pass

    report = trace.report()
    assert report["model_call_count"] == 1
    assert report["tool_call_count"] == 1
    assert report["input_tokens"] == 100
    assert report["output_tokens"] == 20
    assert report["estimated_cost_usd"] > 0


def test_cost_breakdown_marks_known_cost_complete() -> None:
    with trace_agent("test") as trace:
        with trace_model_call("m", model="gpt-4o", input_tokens=1000, output_tokens=100):
            pass

    breakdown = trace.report()["cost_breakdown"]
    assert breakdown["has_unknown_cost"] is False
    assert breakdown["unavailable_model_call_count"] == 0
    assert breakdown["calculated_usd"] > 0
    assert breakdown["known_cost_usd"] == breakdown["calculated_usd"]
    assert breakdown["pricing_as_of"] == ["2025-06-01"]
    assert breakdown["pricing_sources"]


def test_unknown_model_contributes_zero_but_is_flagged_not_defaulted() -> None:
    with trace_agent("test") as trace:
        with trace_model_call(
            "m", model="mystery-local-model", input_tokens=1000, output_tokens=100
        ):
            pass

    report = trace.report()
    # The old fabricated $1/M + $3/M default would have produced ~0.0013 here.
    assert report["estimated_cost_usd"] == 0
    breakdown = report["cost_breakdown"]
    assert breakdown["has_unknown_cost"] is True
    assert breakdown["unavailable_model_call_count"] == 1
    assert breakdown["unknown_models"] == ["mystery-local-model"]


def test_cost_breakdown_splits_calculated_provider_reported_and_unavailable() -> None:
    with trace_agent("test") as trace:
        with trace_model_call("known", model="gpt-4o", input_tokens=1000, output_tokens=100):
            pass
        with trace_model_call("unknown", model="???", input_tokens=1000, output_tokens=100):
            pass
        with trace_model_call(
            "reported",
            model="x",
            input_tokens=10,
            output_tokens=10,
            metadata={"provider_reported_cost_usd": 0.02},
        ):
            pass

    breakdown = trace.report()["cost_breakdown"]
    assert breakdown["calculated_usd"] > 0
    assert breakdown["provider_reported_usd"] == 0.02
    assert breakdown["priced_model_call_count"] == 2
    assert breakdown["unavailable_model_call_count"] == 1
    assert breakdown["has_unknown_cost"] is True
    assert breakdown["known_cost_usd"] == round(
        breakdown["calculated_usd"] + breakdown["provider_reported_usd"], 6
    )


def test_cost_breakdown_honors_provider_metadata_for_resolution() -> None:
    with trace_agent("test") as trace:
        with trace_model_call(
            "m",
            model="gpt-4o-mini",
            input_tokens=1000,
            output_tokens=100,
            metadata={"provider": "openai", "cached_input_tokens": 500},
        ):
            pass

    breakdown = trace.report()["cost_breakdown"]
    assert breakdown["has_unknown_cost"] is False
    call = breakdown["model_calls"][0]
    assert call["provider"] == "openai"
    assert call["cached_input_tokens"] == 500


def test_repeated_context_recommendation() -> None:
    repeated = "same stable instruction " * 100
    with trace_agent("test") as trace:
        with trace_model_call("a", input_text=repeated, output_tokens=1):
            pass
        with trace_model_call("b", input_text=repeated, output_tokens=1):
            pass

    titles = [rec["title"] for rec in trace.report()["recommendations"]]
    assert "Cache repeated context" in titles


def test_cost_status_reflects_completeness() -> None:
    def status_for(model: str) -> str:
        with trace_agent("t") as trace:
            with trace_model_call("m", model=model, input_tokens=100, output_tokens=10):
                pass
        return trace.report()["cost_status"]

    assert status_for("gpt-4o") == "complete"
    assert status_for("unpriced-model") == "unknown"

    with trace_agent("mix") as trace:
        with trace_model_call("a", model="gpt-4o", input_tokens=100, output_tokens=10):
            pass
        with trace_model_call("b", model="unpriced", input_tokens=100, output_tokens=10):
            pass
    assert trace.report()["cost_status"] == "partial"

    with trace_agent("empty") as trace:
        pass
    assert trace.report()["cost_status"] == "empty"


def test_invalid_provider_reported_metadata_does_not_crash_report() -> None:
    for bad in (float("nan"), float("inf"), -3.0, "not-a-number"):
        with trace_agent("t") as trace:
            with trace_model_call(
                "m",
                model="gpt-4o",
                input_tokens=100,
                output_tokens=10,
                metadata={"provider_reported_cost_usd": bad},
            ):
                pass
        report = trace.report()  # must not raise
        assert report["cost_status"] == "unknown"
        assert "invalid_metadata" in report["cost_breakdown"]["unknown_reasons"]


def test_cached_tokens_metadata_is_clamped_to_input() -> None:
    with trace_agent("t") as trace:
        with trace_model_call(
            "m",
            model="gpt-4o",
            input_tokens=100,
            output_tokens=10,
            metadata={"cached_input_tokens": 10_000},
        ):
            pass
    report = trace.report()
    assert report["cost_status"] == "complete"
    assert report["cost_breakdown"]["model_calls"][0]["cached_input_tokens"] == 100
