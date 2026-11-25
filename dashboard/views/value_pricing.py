"""The Value & Pricing page.

Split out of ``dashboard/app.py`` (issue #407).
"""

from __future__ import annotations

import streamlit as st

from agentloop.value import build_value_report

try:
    from dashboard.value_view import assumption_inputs, render_value_report
except ModuleNotFoundError:
    from value_view import assumption_inputs, render_value_report

try:
    from dashboard.common import recent_traces, select_trace
except ModuleNotFoundError:
    from common import recent_traces, select_trace


def render(ctx) -> None:
    store = ctx.store
    project_id = ctx.project_id
    traces = recent_traces(store, project_id)
    if not traces:
        st.info(
            "No traces stored for this project yet. Generate demo traces from the Ingest page first."
        )
    else:
        trace = select_trace(traces, project_id, label="Choose trace for value report")
        if trace is not None:
            st.markdown("#### Assumptions")
            rpm, rate, incident_cost = assumption_inputs("value_page_")
            value = build_value_report(
                trace,
                runs_per_month=rpm,
                engineer_hourly_rate_usd=rate,
                incident_cost_usd=incident_cost,
            )
            render_value_report(value)
            st.subheader("CLI command")
            st.code(
                "uv run agentloop value-report --path runs/research_agent_baseline.json "
                f"--runs-per-month {rpm} "
                f"--engineer-hourly-rate-usd {rate} "
                f"--incident-cost-usd {incident_cost} "
                "--out runs/value_report.json",
                language="bash",
            )
