from __future__ import annotations

from typing import Any

from agentloop.costs import is_cost_evaluable
from agentloop.optimizer import build_optimization_plan


def build_value_report(
    trace: Any,
    *,
    runs_per_month: int = 1000,
    engineer_hourly_rate_usd: float = 150.0,
    incident_cost_usd: float = 500.0,
) -> dict[str, Any]:
    """Translate an optimization plan into operational value metrics.

    The report connects a trace to modeled monthly cost, engineering time, latency,
    and reliability impact. Its assumptions remain explicit so teams can use it for
    capacity planning and optimization prioritization without treating estimates as
    observed financial results.
    """

    if runs_per_month < 0:
        raise ValueError("runs_per_month must be non-negative")
    if engineer_hourly_rate_usd < 0:
        raise ValueError("engineer_hourly_rate_usd must be non-negative")
    if incident_cost_usd < 0:
        raise ValueError("incident_cost_usd must be non-negative")

    plan = build_optimization_plan(trace)
    current = plan["current"]
    estimated_after = plan["estimated_after"]
    cards = plan.get("optimization_cards", [])

    cost_status = plan.get("cost_status", current.get("cost_status", "complete"))
    cost_evaluable = is_cost_evaluable(cost_status)
    current_cost_per_run = float(current.get("estimated_cost_usd", 0.0))
    optimized_cost_raw = estimated_after.get("estimated_cost_usd")
    optimized_cost_per_run = (
        float(optimized_cost_raw) if cost_evaluable and optimized_cost_raw is not None else None
    )
    cost_savings_per_run = (
        max(0.0, current_cost_per_run - optimized_cost_per_run)
        if optimized_cost_per_run is not None
        else None
    )

    current_runtime_ms = float(current.get("runtime_ms", 0.0))
    optimized_runtime_ms = float(estimated_after.get("runtime_ms", current_runtime_ms))
    latency_savings_ms_per_run = max(0.0, current_runtime_ms - optimized_runtime_ms)

    retry_count = int(current.get("retry_count", 0) or 0)
    reliability_risk_score = _risk_score(current=current, cards=cards)
    avoided_incident_value = _avoided_incident_value(
        retry_count=retry_count,
        reliability_risk_score=reliability_risk_score,
        incident_cost_usd=incident_cost_usd,
        runs_per_month=runs_per_month,
    )

    engineering_hours_saved_monthly = _engineering_hours_saved(cards, runs_per_month)
    engineering_value_monthly = engineering_hours_saved_monthly * engineer_hourly_rate_usd
    direct_model_cost_savings_monthly = (
        cost_savings_per_run * runs_per_month if cost_savings_per_run is not None else None
    )
    latency_hours_saved_monthly = (latency_savings_ms_per_run * runs_per_month) / 3_600_000
    non_cost_value_monthly = engineering_value_monthly + avoided_incident_value
    total_value_monthly = (
        direct_model_cost_savings_monthly + non_cost_value_monthly
        if direct_model_cost_savings_monthly is not None
        else None
    )
    pricing = (
        _pricing_scenario(
            monthly_value=total_value_monthly,
            runs_per_month=runs_per_month,
            reliability_risk_score=reliability_risk_score,
            cards=cards,
        )
        if total_value_monthly is not None
        else _unavailable_pricing_scenario(non_cost_value_monthly)
    )

    return {
        "run_id": plan.get("run_id"),
        "name": plan.get("name"),
        "cost_status": cost_status,
        "assumptions": {
            "runs_per_month": runs_per_month,
            "engineer_hourly_rate_usd": engineer_hourly_rate_usd,
            "incident_cost_usd": incident_cost_usd,
        },
        "current": current,
        "estimated_after": estimated_after,
        "per_run": {
            "cost_savings_usd": (
                None if cost_savings_per_run is None else round(cost_savings_per_run, 6)
            ),
            "latency_savings_ms": round(latency_savings_ms_per_run, 3),
            "latency_reduction_pct": estimated_after.get("latency_reduction_pct", 0.0),
            "cost_reduction_pct": estimated_after.get("cost_reduction_pct", 0.0),
        },
        "monthly_value": {
            "direct_model_cost_savings_usd": (
                None
                if direct_model_cost_savings_monthly is None
                else round(direct_model_cost_savings_monthly, 2)
            ),
            "engineering_hours_saved": round(engineering_hours_saved_monthly, 2),
            "engineering_value_usd": round(engineering_value_monthly, 2),
            "latency_hours_saved": round(latency_hours_saved_monthly, 2),
            "avoided_incident_value_usd": round(avoided_incident_value, 2),
            "non_cost_operational_value_usd": round(non_cost_value_monthly, 2),
            "total_value_usd": (
                None if total_value_monthly is None else round(total_value_monthly, 2)
            ),
        },
        "pricing": pricing,
        "reliability": {
            "risk_score": reliability_risk_score,
            "retry_count": retry_count,
            "high_confidence_fixes": sum(1 for card in cards if card.get("confidence") == "high"),
            "top_risks": _top_risks(cards),
        },
        "value_summary": _value_summary(
            monthly_value=total_value_monthly,
            latency_savings_ms_per_run=latency_savings_ms_per_run,
            cost_savings_per_run=cost_savings_per_run,
            cards=cards,
            pricing=pricing,
        ),
        "optimization_cards": cards,
    }


def _risk_score(*, current: dict[str, Any], cards: list[dict[str, Any]]) -> int:
    score = 0
    retry_count = int(current.get("retry_count", 0) or 0)
    repeated_context_ratio = float(current.get("repeated_context_ratio", 0.0) or 0.0)
    input_tokens = int(current.get("input_tokens", 0) or 0)

    score += min(35, retry_count * 12)
    score += min(25, int(repeated_context_ratio * 100))
    score += min(20, input_tokens // 5000)
    score += min(20, len(cards) * 4)
    return max(0, min(100, score))


def _engineering_hours_saved(cards: list[dict[str, Any]], runs_per_month: int) -> float:
    if not cards or runs_per_month == 0:
        return 0.0

    # Conservative proxy: repeated production inefficiencies create triage and refactor
    # work. Cap it to avoid turning this into a fake precision calculator.
    base_hours = min(12.0, len(cards) * 1.25)
    scale = min(3.0, max(0.25, runs_per_month / 1000))
    high_confidence_bonus = sum(0.5 for card in cards if card.get("confidence") == "high")
    return min(40.0, (base_hours + high_confidence_bonus) * scale)


def _avoided_incident_value(
    *,
    retry_count: int,
    reliability_risk_score: int,
    incident_cost_usd: float,
    runs_per_month: int,
) -> float:
    if runs_per_month == 0 or incident_cost_usd == 0:
        return 0.0
    monthly_failure_probability = min(0.25, (retry_count * 0.01) + (reliability_risk_score / 1000))
    volume_multiplier = min(2.0, max(0.25, runs_per_month / 1000))
    return incident_cost_usd * monthly_failure_probability * volume_multiplier


def _pricing_scenario(
    *,
    monthly_value: float,
    runs_per_month: int,
    reliability_risk_score: int,
    cards: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a conservative pricing scenario from modeled operational value.

    This is not a billing system or price quote. It provides a configurable tier,
    monthly amount, and value-to-price ratio for planning comparisons.
    """

    if runs_per_month == 0 or monthly_value <= 0 or not cards:
        suggested_plan = "free"
        suggested_monthly_price_usd = 0
        rationale = "No meaningful optimization value was detected for this trace."
    elif monthly_value < 750 and runs_per_month < 1000:
        suggested_plan = "pro"
        suggested_monthly_price_usd = 49
        rationale = "Low-volume workload with measurable optimization value."
    elif monthly_value < 5000 and reliability_risk_score < 60:
        suggested_plan = "team"
        suggested_monthly_price_usd = 499
        rationale = "Mid-volume workload with shared tracing and optimization needs."
    elif monthly_value < 25000:
        suggested_plan = "growth"
        suggested_monthly_price_usd = 1500
        rationale = "Higher-volume workload with material reliability and latency impact."
    else:
        suggested_plan = "enterprise"
        suggested_monthly_price_usd = 5000
        rationale = (
            "Large modeled value; review deployment, retention, support, and usage requirements."
        )

    value_to_price_ratio = None
    if suggested_monthly_price_usd > 0:
        value_to_price_ratio = round(monthly_value / suggested_monthly_price_usd, 2)

    return {
        "suggested_plan": suggested_plan,
        "suggested_monthly_price_usd": suggested_monthly_price_usd,
        "estimated_monthly_value_usd": round(monthly_value, 2),
        "value_to_price_ratio": value_to_price_ratio,
        "rationale": rationale,
        "scenario_notes": _scenario_notes(suggested_plan),
    }


def _unavailable_pricing_scenario(non_cost_value: float) -> dict[str, Any]:
    return {
        "suggested_plan": None,
        "suggested_monthly_price_usd": None,
        "estimated_monthly_value_usd": None,
        "estimated_non_cost_monthly_value_usd": round(non_cost_value, 2),
        "value_to_price_ratio": None,
        "rationale": "Pricing is unavailable because one or more model-call costs are unknown.",
        "scenario_notes": [],
    }


def _scenario_notes(plan: str) -> list[str]:
    if plan == "free":
        return ["Local traces", "CLI reports", "Manual optimization plan export"]
    if plan == "pro":
        return ["Local + API trace upload", "Value reports", "Single project workspace"]
    if plan == "team":
        return ["Shared dashboard", "API keys", "Shared projects", "Optimization reports"]
    if plan == "growth":
        return [
            "Higher usage limits",
            "Postgres backend",
            "Extended retention",
            "Operational value review",
        ]
    return ["Private deployment", "Custom retention", "Security review", "Custom integrations"]


def _top_risks(cards: list[dict[str, Any]]) -> list[str]:
    risks = []
    for card in cards[:3]:
        title = str(card.get("title", "Optimization opportunity"))
        confidence = str(card.get("confidence", "unknown"))
        risks.append(f"{title} ({confidence} confidence)")
    return risks


def _value_summary(
    *,
    monthly_value: float | None,
    latency_savings_ms_per_run: float,
    cost_savings_per_run: float | None,
    cards: list[dict[str, Any]],
    pricing: dict[str, Any],
) -> str:
    strongest_fix = cards[0]["title"] if cards else "No major bottleneck detected"
    if monthly_value is None or cost_savings_per_run is None:
        return (
            f"AgentLoop found {len(cards)} optimization opportunities. "
            f"The strongest immediate fix is: {strongest_fix}. "
            f"Estimated latency savings are {latency_savings_ms_per_run / 1000:.2f}s per run. "
            "Model-cost savings, total monthly value, and pricing are unavailable because "
            "one or more model-call costs could not be priced."
        )

    price = pricing.get("suggested_monthly_price_usd", 0)
    plan = pricing.get("suggested_plan", "free")
    price_line = "free local usage" if price == 0 else f"the {plan} plan at ${price:,.0f}/month"
    return (
        f"AgentLoop found {len(cards)} optimization opportunities. "
        f"The strongest immediate fix is: {strongest_fix}. "
        f"At the provided usage assumptions, this trace represents about "
        f"${monthly_value:,.0f}/month in modeled value, with "
        f"{latency_savings_ms_per_run / 1000:.2f}s latency saved and "
        f"${cost_savings_per_run:.4f} model cost saved per run. "
        f"A modeled pricing scenario is {price_line}."
    )
