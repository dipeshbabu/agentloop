"""The Overview page.

Split out of ``dashboard/app.py`` (issue #407).
"""

from __future__ import annotations

import altair as alt
import streamlit as st

from agentloop.costs import format_cost_usd

try:
    from dashboard.common import (
        RECENT_TRACE_LIMIT,
        recent_traces,
        render_runs_table,
        runs_frame,
        seconds,
    )
except ModuleNotFoundError:
    from common import (
        RECENT_TRACE_LIMIT,
        recent_traces,
        render_runs_table,
        runs_frame,
        seconds,
    )


def render(ctx) -> None:
    store = ctx.store
    project_id = ctx.project_id
    summary = store.usage_summary(project_id=project_id)
    traces = recent_traces(store, project_id)

    run_count = int(summary.get("run_count", 0))
    total_runtime_ms = summary.get("total_runtime_ms", 0) or 0
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Runs", f"{run_count:,}")
    c2.metric(
        "Total runtime",
        seconds(total_runtime_ms),
        help=f"Average {seconds(total_runtime_ms / run_count)} per run" if run_count else None,
    )
    c3.metric(
        "Estimated LLM cost",
        format_cost_usd(summary.get("estimated_cost_usd"), summary.get("cost_status", "complete")),
    )
    c4.metric("Retries", f"{int(summary.get('retry_count', 0)):,}")

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Input tokens", f"{int(summary.get('input_tokens', 0)):,}")
    c6.metric("Output tokens", f"{int(summary.get('output_tokens', 0)):,}")
    c7.metric("Model calls", f"{int(summary.get('model_call_count', 0)):,}")
    c8.metric("Tool calls", f"{int(summary.get('tool_call_count', 0)):,}")

    if traces:
        runs_df = runs_frame(traces)

        st.subheader("Recent runs")
        if len(traces) >= RECENT_TRACE_LIMIT:
            st.caption(
                f"The newest {RECENT_TRACE_LIMIT} runs. Browse every run on the Traces page."
            )
        render_runs_table(runs_df)

        timeline, by_agent = st.columns([3, 2], gap="large")
        with timeline:
            st.subheader("Runtime over time")
            st.altair_chart(
                alt.Chart(runs_df.dropna(subset=["started"]))
                .mark_line(point=alt.OverlayMarkDef(size=45), strokeWidth=2)
                .encode(
                    x=alt.X("started:T", title=None),
                    y=alt.Y("runtime_s:Q", title="Runtime (s)"),
                    color=alt.Color(
                        "name:N",
                        title="Agent",
                        legend=alt.Legend(orient="bottom", columns=2, labelLimit=260),
                    ),
                    tooltip=[
                        alt.Tooltip("name:N", title="Agent"),
                        alt.Tooltip("started:T", title="Started", format="%b %d, %H:%M:%S"),
                        alt.Tooltip("runtime_s:Q", title="Runtime (s)", format=".2f"),
                        alt.Tooltip("run_id:N", title="Run ID"),
                    ],
                )
                .properties(height=300),
                width="stretch",
            )
        with by_agent:
            st.subheader("Runs by agent")
            agents = (
                runs_df.groupby("name", as_index=False)
                .agg(
                    runs=("run_id", "count"),
                    average_s=("runtime_s", "mean"),
                    cost=("estimated_cost_usd", "sum"),
                )
                .sort_values("runs", ascending=False)
            )
            st.altair_chart(
                alt.Chart(agents)
                .mark_bar(cornerRadiusEnd=4, height=18)
                .encode(
                    x=alt.X("runs:Q", title="Runs"),
                    y=alt.Y("name:N", title=None, sort="-x", axis=alt.Axis(labelLimit=220)),
                    color=alt.Color("name:N", legend=None),
                    tooltip=[
                        alt.Tooltip("name:N", title="Agent"),
                        alt.Tooltip("runs:Q", title="Runs"),
                        alt.Tooltip("average_s:Q", title="Average runtime (s)", format=".2f"),
                        alt.Tooltip("cost:Q", title="Cost (USD)", format="$.4f"),
                    ],
                )
                .properties(height=300),
                width="stretch",
            )
    else:
        st.info(
            "No stored traces yet. Use the Ingest page or run "
            "`uv run agentloop store-trace --path TRACE.json`."
        )
