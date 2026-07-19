from agentloop.demo import run_baseline
from agentloop.tracer import AgentTrace, trace_agent, trace_model_call
from agentloop.value import build_value_report


def test_value_report_has_operational_metrics(tmp_path):
    path = run_baseline(tmp_path)
    trace = AgentTrace.from_json(path)

    report = build_value_report(trace, runs_per_month=2000, engineer_hourly_rate_usd=125)

    assert report["run_id"] == trace.run_id
    assert report["monthly_value"]["total_value_usd"] >= 0
    assert report["per_run"]["latency_savings_ms"] >= 0
    assert 0 <= report["reliability"]["risk_score"] <= 100
    assert "value_summary" in report


def test_value_report_includes_pricing_scenario(tmp_path):
    path = run_baseline(tmp_path)
    trace = AgentTrace.from_json(path)

    report = build_value_report(
        trace,
        runs_per_month=5000,
        engineer_hourly_rate_usd=150,
        incident_cost_usd=1000,
    )

    pricing = report["pricing"]
    assert pricing["suggested_plan"] in {"free", "pro", "team", "growth", "enterprise"}
    assert pricing["suggested_monthly_price_usd"] >= 0
    assert pricing["estimated_monthly_value_usd"] == report["monthly_value"]["total_value_usd"]
    assert isinstance(pricing["scenario_notes"], list)


def test_value_report_rejects_negative_assumptions(tmp_path):
    path = run_baseline(tmp_path)
    trace = AgentTrace.from_json(path)

    try:
        build_value_report(trace, runs_per_month=-1)
    except ValueError as exc:
        assert "runs_per_month" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_partial_cost_value_report_withholds_totals_and_pricing():
    repeated = "stable context " * 100
    with trace_agent("partial-value") as trace:
        with trace_model_call(
            "known",
            model="gpt-4o",
            input_tokens=200,
            output_tokens=10,
            input_text=repeated,
        ):
            pass
        with trace_model_call(
            "unknown",
            model="private-model",
            input_tokens=200,
            output_tokens=10,
            input_text=repeated,
        ):
            pass

    report = build_value_report(trace, runs_per_month=1000)

    assert report["cost_status"] == "partial"
    assert report["per_run"]["cost_savings_usd"] is None
    assert report["monthly_value"]["direct_model_cost_savings_usd"] is None
    assert report["monthly_value"]["total_value_usd"] is None
    assert report["pricing"]["suggested_plan"] is None
    assert report["pricing"]["suggested_monthly_price_usd"] is None
    assert "unavailable" in report["value_summary"]
