from __future__ import annotations

from typing import Any

from agentloop.optimizer import build_optimization_plan


def build_value_report(
    trace: Any,
    *,
    runs_per_month: int = 1000,
    engineer_hourly_rate_usd: float = 150.0,
    incident_cost_usd: float = 500.0,
) -> dict[str, Any]:
    """Translate an optimization plan into buyer-facing ROI and sales metrics.

    AgentLoop is easiest to sell when a team can see the monthly dollars, engineering
    time, and reliability risk tied to a trace. This report deliberately keeps the math
    transparent and conservative so it can be used in pilots, investor demos, and sales
    calls without overstating impact.
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

    current_cost_per_run = float(current.get("estimated_cost_usd", 0.0))
    optimized_cost_per_run = float(estimated_after.get("estimated_cost_usd", current_cost_per_run))
    cost_savings_per_run = max(0.0, current_cost_per_run - optimized_cost_per_run)

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
    direct_model_cost_savings_monthly = cost_savings_per_run * runs_per_month
    latency_hours_saved_monthly = (latency_savings_ms_per_run * runs_per_month) / 3_600_000
    total_value_monthly = (
        direct_model_cost_savings_monthly + engineering_value_monthly + avoided_incident_value
    )

    return {
        "run_id": plan.get("run_id"),
        "name": plan.get("name"),
        "assumptions": {
            "runs_per_month": runs_per_month,
            "engineer_hourly_rate_usd": engineer_hourly_rate_usd,
            "incident_cost_usd": incident_cost_usd,
        },
        "current": current,
        "estimated_after": estimated_after,
        "per_run": {
            "cost_savings_usd": round(cost_savings_per_run, 6),
            "latency_savings_ms": round(latency_savings_ms_per_run, 3),
            "latency_reduction_pct": estimated_after.get("latency_reduction_pct", 0.0),
            "cost_reduction_pct": estimated_after.get("cost_reduction_pct", 0.0),
        },
        "monthly_value": {
            "direct_model_cost_savings_usd": round(direct_model_cost_savings_monthly, 2),
            "engineering_hours_saved": round(engineering_hours_saved_monthly, 2),
            "engineering_value_usd": round(engineering_value_monthly, 2),
            "latency_hours_saved": round(latency_hours_saved_monthly, 2),
            "avoided_incident_value_usd": round(avoided_incident_value, 2),
            "total_value_usd": round(total_value_monthly, 2),
        },
        "reliability": {
            "risk_score": reliability_risk_score,
            "retry_count": retry_count,
            "high_confidence_fixes": sum(1 for card in cards if card.get("confidence") == "high"),
            "top_risks": _top_risks(cards),
        },
        "sales_summary": _sales_summary(
            monthly_value=total_value_monthly,
            latency_savings_ms_per_run=latency_savings_ms_per_run,
            cost_savings_per_run=cost_savings_per_run,
            cards=cards,
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


def _top_risks(cards: list[dict[str, Any]]) -> list[str]:
    risks = []
    for card in cards[:3]:
        title = str(card.get("title", "Optimization opportunity"))
        confidence = str(card.get("confidence", "unknown"))
        risks.append(f"{title} ({confidence} confidence)")
    return risks


def _sales_summary(
    *,
    monthly_value: float,
    latency_savings_ms_per_run: float,
    cost_savings_per_run: float,
    cards: list[dict[str, Any]],
) -> str:
    strongest_fix = cards[0]["title"] if cards else "No major bottleneck detected"
    return (
        f"AgentLoop found {len(cards)} optimization opportunities. "
        f"The strongest immediate fix is: {strongest_fix}. "
        f"At the provided usage assumptions, this trace represents about "
        f"${monthly_value:,.0f}/month in modeled value, with "
        f"{latency_savings_ms_per_run / 1000:.2f}s latency saved and "
        f"${cost_savings_per_run:.4f} model cost saved per run."
    )
