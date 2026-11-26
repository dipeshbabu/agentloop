"""The Optimization page.

Split out of ``dashboard/app.py`` (issue #407).
"""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from agentloop.optimizer import build_optimization_plan
from agentloop.value import build_value_report

try:
    from dashboard.value_view import (
        assumption_inputs,
        percentage,
        render_value_report,
    )
except ModuleNotFoundError:
    from value_view import (
        assumption_inputs,
        percentage,
        render_value_report,
    )

try:
    from dashboard.common import (
        money,
        recent_traces,
        render_estimate_details,
        seconds,
        select_trace,
    )
except ModuleNotFoundError:
    from common import (
        money,
        recent_traces,
        render_estimate_details,
        seconds,
        select_trace,
    )


def render(ctx) -> None:
    store = ctx.store
    project_id = ctx.project_id
    traces = recent_traces(store, project_id)
    if not traces:
        st.info("No traces stored for this project yet.")
    else:
        trace = select_trace(traces, project_id)
        if trace is not None:
            report = trace.report()
            plan = build_optimization_plan(trace, report)
            if (
                plan.get("savings_aggregation", {}).get("latency_estimate_complete") is False
                or plan.get("savings_aggregation", {}).get("cost_estimate_complete") is False
            ):
                st.caption("Some savings are unavailable; totals cover modeled candidates only.")
            current = plan["current"]
            after = plan["estimated_after"]

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Current runtime", seconds(current["runtime_ms"]))
            c2.metric("Estimated runtime", seconds(after["runtime_ms"]))
            c3.metric("Latency reduction", f"{after['latency_reduction_pct']:.1f}%")
            c4.metric("Cost reduction", percentage(after["cost_reduction_pct"]))

            with st.expander("Show value and pricing estimate", expanded=False):
                rpm, rate, incident_cost = assumption_inputs("optimization_")
                value = build_value_report(
                    trace,
                    runs_per_month=rpm,
                    engineer_hourly_rate_usd=rate,
                    incident_cost_usd=incident_cost,
                )
                render_value_report(value, show_download=False)

            st.subheader("Optimization cards")
            if plan.get("rule_errors"):
                st.warning(
                    "Analysis incomplete; failed rules: "
                    + ", ".join(error["rule_id"] for error in plan["rule_errors"])
                )
            cards = plan["optimization_cards"]
            if not cards:
                st.info("No major optimization cards detected yet.")
            for card in cards:
                with st.expander(
                    f"{card['title']} · confidence: {card['confidence']}", expanded=True
                ):
                    st.write(card["why"])
                    render_estimate_details(card)
                    st.code(card["rewrite_hint"])
                    st.write(
                        f"Estimated latency savings: {seconds(card['estimated_latency_savings_ms'])}"
                    )
                    st.write(f"Estimated cost savings: {money(card['estimated_cost_savings_usd'])}")

            st.subheader("Execution graph")
            graph = plan["graph"]
            if graph.get("execution") is not None:
                st.json(graph["execution"])
            if graph.get("dependency_evidence") is not None:
                st.json(graph["dependency_evidence"])
            st.write("Bottlenecks")
            st.dataframe(pd.DataFrame(graph["bottlenecks"]), width="stretch", hide_index=True)
            st.write("Parallelization candidates")
            if graph["parallelizable_groups"]:
                st.dataframe(
                    pd.DataFrame(graph["parallelizable_groups"]), width="stretch", hide_index=True
                )
            else:
                st.info(
                    "No repeated tool-call groups currently qualify as parallelization candidates."
                )
            st.write("Edges")
            st.dataframe(pd.DataFrame(graph["edges"]), width="stretch", hide_index=True)

            st.download_button(
                "Download optimization plan JSON",
                data=json.dumps(plan, indent=2),
                file_name="agentloop_optimization_plan.json",
                mime="application/json",
            )
