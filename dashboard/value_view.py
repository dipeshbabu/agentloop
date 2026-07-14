from __future__ import annotations

import json
from typing import Any


def money(value: float, decimals: int = 2) -> str:
    return f"${float(value):,.{decimals}f}"


def seconds(ms: float) -> str:
    return f"{float(ms) / 1000:,.2f}s"


def _streamlit() -> Any:
    try:
        import streamlit as st
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "Install dashboard support with: uv sync --locked --extra dashboard"
        ) from exc
    return st


def assumption_inputs(prefix: str = "") -> tuple[int, float, float]:
    st = _streamlit()
    c1, c2, c3 = st.columns(3)
    runs_per_month = c1.number_input(
        "Runs per month",
        min_value=0,
        value=5000,
        step=500,
        key=f"{prefix}runs_per_month",
    )
    engineer_hourly_rate_usd = c2.number_input(
        "Engineer hourly rate",
        min_value=0.0,
        value=150.0,
        step=25.0,
        key=f"{prefix}engineer_hourly_rate_usd",
    )
    incident_cost_usd = c3.number_input(
        "Reliability event cost",
        min_value=0.0,
        value=500.0,
        step=100.0,
        key=f"{prefix}incident_cost_usd",
    )
    return int(runs_per_month), float(engineer_hourly_rate_usd), float(incident_cost_usd)


def render_value_report(value: dict, *, show_download: bool = True) -> None:
    st = _streamlit()
    monthly = value["monthly_value"]
    per_run = value["per_run"]
    pricing = value.get("pricing", {})
    reliability = value["reliability"]

    st.subheader("Operational value")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Monthly value", money(monthly["total_value_usd"]))
    c2.metric("Cost savings / month", money(monthly["direct_model_cost_savings_usd"]))
    c3.metric("Engineering hours saved", monthly["engineering_hours_saved"])
    c4.metric("Latency hours saved", monthly["latency_hours_saved"])

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Latency saved / run", seconds(per_run["latency_savings_ms"]))
    c6.metric("Cost saved / run", money(per_run["cost_savings_usd"], decimals=6))
    c7.metric("Reliability risk", f"{reliability['risk_score']}/100")
    c8.metric("High confidence fixes", reliability["high_confidence_fixes"])

    st.subheader("Pricing scenario")
    p1, p2, p3, p4 = st.columns(4)
    p1.metric("Modeled tier", pricing.get("suggested_plan", "n/a"))
    p2.metric("Modeled price", money(pricing.get("suggested_monthly_price_usd", 0), decimals=0))
    ratio = pricing.get("value_to_price_ratio")
    p3.metric("Value / price", "n/a" if ratio is None else f"{ratio}x")
    p4.metric("Monthly value", money(pricing.get("estimated_monthly_value_usd", 0)))

    st.info(pricing.get("rationale", "No pricing rationale available."))

    notes = pricing.get("scenario_notes", [])
    if notes:
        st.markdown("#### Scenario includes")
        st.write("\n".join(f"- {note}" for note in notes))

    st.subheader("Value summary")
    st.write(value["value_summary"])

    risks = reliability.get("top_risks", [])
    if risks:
        st.subheader("Top reliability risks")
        st.write("\n".join(f"- {risk}" for risk in risks))

    if show_download:
        st.download_button(
            "Download value report JSON",
            data=json.dumps(value, indent=2),
            file_name="agentloop_value_report.json",
            mime="application/json",
        )
