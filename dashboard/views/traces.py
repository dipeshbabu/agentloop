"""The Traces page.

Split out of ``dashboard/app.py`` (issue #407).
"""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from agentloop.costs import format_cost_usd
from agentloop.operations import operation_kind
from agentloop.store import (
    parse_label_filter,
)

try:
    from dashboard.common import (
        invalidate_cached_results,
        load_trace_for_project,
        render_runs_table,
        render_waterfall,
        runs_frame,
        seconds,
    )
except ModuleNotFoundError:
    from common import (
        invalidate_cached_results,
        load_trace_for_project,
        render_runs_table,
        render_waterfall,
        runs_frame,
        seconds,
    )


def render(ctx) -> None:
    store = ctx.store
    project_id = ctx.project_id
    dashboard_actor = ctx.dashboard_actor
    if notice := st.session_state.pop("trace_deleted_notice", None):
        st.success(notice)

    filter_col, size_col = st.columns([4, 1])
    label_text = filter_col.text_input(
        "Filter by label",
        placeholder="KEY=VALUE, for example commit=abc123 or condition=candidate",
    ).strip()
    label_filter = None
    if label_text:
        try:
            label_filter = parse_label_filter(label_text)
        except ValueError as exc:
            st.error(f"{exc}. Showing all traces.")

    cursor_key = f"traces_cursor_stack::{project_id}::{label_filter}"
    if cursor_key not in st.session_state:
        st.session_state[cursor_key] = [None]  # cursor to fetch the *current* page

    page_index = len(st.session_state[cursor_key]) - 1
    page_size = size_col.number_input("Page size", min_value=5, max_value=200, value=25, step=5)
    page = store.list_traces_page(
        project_id=project_id,
        limit=int(page_size),
        cursor=st.session_state[cursor_key][-1],
        label=label_filter,
    )
    traces = page["items"]

    if not traces:
        st.info(
            f"No traces labelled {label_filter[0]}={label_filter[1]} in this project."
            if label_filter
            else "No traces stored for this project yet."
        )
    else:
        df = runs_frame(traces)
        render_runs_table(df, max_height=420)
        nav_cols = st.columns([1, 1, 1, 5], vertical_alignment="center")
        if nav_cols[0].button(
            "Previous", icon=":material/chevron_left:", disabled=page_index == 0, width="stretch"
        ):
            st.session_state[cursor_key].pop()
            st.rerun()
        if nav_cols[1].button(
            "Next",
            icon=":material/chevron_right:",
            disabled=page["next_cursor"] is None,
            width="stretch",
        ):
            st.session_state[cursor_key].append(page["next_cursor"])
            st.rerun()
        nav_cols[2].caption(f"Page {page_index + 1}")

        st.divider()
        run_names = dict(zip(df["run_id"], df["name"]))
        selected_run = st.selectbox(
            "Open run",
            df["run_id"].tolist(),
            format_func=lambda run_id: f"{run_names[run_id]} · {run_id}",
        )
        trace = load_trace_for_project(selected_run, project_id)
        if trace is not None:
            report = trace.report()
            st.subheader(trace.name)
            if report.get("execution") is not None:
                execution = report["execution"]
                st.caption(
                    f"Workflow {execution.get('workflow_id', 'unknown')} · version {execution.get('version', 'unknown')} · {execution.get('status', 'unknown')}"
                )
                with st.expander("Workflow references"):
                    st.json(execution)
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Runtime", seconds(report["total_runtime_ms"]))
            c2.metric(
                "Recorded model cost" if "execution" in report else "Cost",
                format_cost_usd(report["estimated_cost_usd"], report.get("cost_status")),
            )
            c3.metric("Model calls", report["model_call_count"])
            c4.metric("Tool calls", report["tool_call_count"])

            events = pd.DataFrame(report["events"])
            st.subheader("Event timeline")
            render_waterfall(events)
            if not events.empty:
                cols = [
                    "event_type",
                    "name",
                    "duration_ms",
                    "model",
                    "input_tokens",
                    "output_tokens",
                    "status",
                ]
                if "execution" in report or "stages" in report:
                    events["operation_kind"] = [operation_kind(event) for event in trace.events]
                    summaries = [
                        report.get("stages", {}).get(event.event_id, {}) for event in trace.events
                    ]
                    stage_columns = {
                        "Stage": "stage_id",
                        "Stage kind": "kind",
                        "Stage version": "version",
                        "Input schema": "input_schema_ref",
                        "Output schema": "output_schema_ref",
                        "Input reference": "input_ref",
                        "Output reference": "output_ref",
                        "Outcome": "outcome",
                        "Dependencies": "depends_on",
                    }
                    for label, key in stage_columns.items():
                        events[label] = [value.get(key) for value in summaries]
                    cols = [
                        "operation_kind",
                        "Stage",
                        "Stage kind",
                        "Stage version",
                        "parent_id",
                        *cols,
                        "Input schema",
                        "Output schema",
                        "Input reference",
                        "Output reference",
                        "Outcome",
                        "Dependencies",
                    ]
                for col in cols:
                    if col not in events.columns:
                        events[col] = None
                st.dataframe(
                    events[cols],
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "event_type": st.column_config.TextColumn("Type"),
                        "name": st.column_config.TextColumn("Name"),
                        "duration_ms": st.column_config.NumberColumn("Duration", format="%.1f ms"),
                        "model": st.column_config.TextColumn("Model"),
                        "input_tokens": st.column_config.NumberColumn("Input tokens"),
                        "output_tokens": st.column_config.NumberColumn("Output tokens"),
                        "status": st.column_config.TextColumn("Status"),
                        "operation_kind": st.column_config.TextColumn("Operation"),
                        "parent_id": st.column_config.TextColumn("Parent"),
                    },
                )

            st.download_button(
                "Download trace JSON",
                data=json.dumps(trace.to_dict(), indent=2),
                file_name=f"{trace.run_id}.json",
                mime="application/json",
            )

            with st.expander("Delete this trace"):
                st.caption(
                    "Permanently removes the trace with its findings, usage records and any "
                    "interventions that compare it. This cannot be undone."
                )
                confirmed = st.checkbox(
                    f"I want to delete {trace.run_id}", key=f"confirm_delete::{trace.run_id}"
                )
                if st.button("Delete trace", disabled=not confirmed, type="primary"):
                    deleted = store.delete_trace(trace.run_id, project_id, actor=dashboard_actor)
                    invalidate_cached_results()
                    st.session_state[cursor_key] = [None]
                    st.session_state["trace_deleted_notice"] = (
                        f"Deleted {trace.run_id} and {deleted['findings']} findings, "
                        f"{deleted['interventions']} interventions."
                        if deleted
                        else f"{trace.run_id} was already deleted."
                    )
                    st.rerun()
