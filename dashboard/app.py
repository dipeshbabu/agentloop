from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from agentloop.ci import build_ci_report, ci_report_to_markdown
from agentloop.ci_runs import CIRunNotFoundError, ci_run_pair, list_ci_runs
from agentloop.costs import format_cost_usd
from agentloop.demo import run_baseline, run_langgraph_style, run_optimized, run_proof_pair
from agentloop.findings import build_diagnosis
from agentloop.issues import build_issue_drafts, issue_drafts_to_markdown
from agentloop.operations import operation_kind
from agentloop.optimizer import build_optimization_plan
from agentloop.patches import RepositoryPathError, build_patch_plan
from agentloop.quality import (
    QualityValidationError,
    build_quality_report,
    parse_quality_fixtures,
    quality_report_to_markdown,
)
from agentloop.ranking import SORT_FIELDS, sort_ranked
from agentloop.replay import ReplayGates, build_replay_report
from agentloop.store import (
    ALLOWED_FINDING_TRANSITIONS,
    MAX_PAGE_SIZE,
    FindingNotFoundError,
    FindingTransitionError,
    get_store,
    parse_label_filter,
)
from agentloop.tracer import AgentTrace
from agentloop.value import build_value_report

try:
    from dashboard.access import (
        lockout_remaining,
        require_project_key,
        session_valid,
        sign_in,
        sign_out,
        visible_pages,
    )
    from dashboard.theme import PAGE_NAMES, apply_styles, nav_label, page_header
    from dashboard.trace_upload import TraceUploadError, parse_uploaded_trace
    from dashboard.value_view import assumption_inputs, percentage, render_value_report
except ModuleNotFoundError:
    from access import (
        lockout_remaining,
        require_project_key,
        session_valid,
        sign_in,
        sign_out,
        visible_pages,
    )
    from theme import PAGE_NAMES, apply_styles, nav_label, page_header
    from trace_upload import TraceUploadError, parse_uploaded_trace
    from value_view import assumption_inputs, percentage, render_value_report

st.set_page_config(
    page_title="AgentLoop",
    page_icon=str(Path(__file__).with_name("assets") / "agentloop-mark.svg"),
    layout="wide",
)
apply_styles()


def _runs_directory() -> Path:
    """Where demo generators write trace files.

    AGENTLOOP_DASHBOARD_RUNS_DIR wins when set. Otherwise the dashboard uses
    ./runs, falling back to a temporary directory when the working directory is
    read-only, as it is for the non-root user in the container image.
    """
    configured = os.getenv("AGENTLOOP_DASHBOARD_RUNS_DIR")
    directory = Path(configured) if configured else Path("runs")
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        if configured:
            raise
        directory = Path(tempfile.gettempdir()) / "agentloop-dashboard-runs"
        directory.mkdir(parents=True, exist_ok=True)
    return directory


runs_dir = _runs_directory()


@st.cache_resource
def load_store():
    db = get_store()
    db.init()
    return db


SEVERITY_COLORS = {"high": "red", "medium": "orange", "low": "gray"}


def money(value: float | None) -> str:
    return "unavailable" if value is None else f"${float(value):,.4f}"


def seconds(ms: float | None) -> str:
    return "unavailable" if ms is None else f"{float(ms) / 1000:,.2f}s"


def render_estimate_details(item: dict) -> None:
    if item.get("evidence_level"):
        st.write(f"Evidence level: {item['evidence_level']}")
    estimate = item.get("estimate")
    if estimate:
        calibration = "calibrated" if estimate.get("calibrated") is True else "uncalibrated"
        st.write(
            f"Estimator: {estimate['estimator_id']} version {estimate['estimator_version']} "
            f"({estimate['method']}, {calibration}; predicted savings)"
        )
        st.write("Assumptions: " + "; ".join(estimate.get("assumptions", [])))
        st.caption(estimate["formula"])
        st.json(
            {"parameters": estimate.get("parameters", {}), "inputs": estimate.get("inputs", {})}
        )
    elif item.get("evidence_level"):
        st.write("Assumptions: " + "; ".join(item.get("assumptions", [])))
        st.caption(item.get("estimate_formula") or item.get("savings", {}).get("formula", ""))


def load_trace_for_project(run_id: str, project_id: str) -> AgentTrace | None:
    return load_store().get_trace(run_id=run_id, project_id=project_id)


def recent_traces() -> list[dict]:
    """The project's newest runs, newest first.

    Tables, charts and trace pickers show only these, so a project with
    thousands of runs does not send every one of them to the browser.
    """
    return store.list_traces_page(project_id=project_id, limit=RECENT_TRACE_LIMIT)["items"]


def runs_frame(traces: list[dict]) -> pd.DataFrame:
    """Trace rows with a parsed start time and runtime in seconds."""
    df = pd.DataFrame(traces)
    df["started"] = pd.to_datetime(df["started_at"], errors="coerce", utc=True)
    df["runtime_s"] = df["total_runtime_ms"] / 1000
    if "labels" in df:
        df["labels"] = [
            ", ".join(f"{key}={value}" for key, value in sorted(labels.items()))
            if isinstance(labels, dict)
            else (labels or "")
            for labels in df["labels"]
        ]
    return df


def render_runs_table(df: pd.DataFrame, *, max_height: int = 390) -> None:
    """A compact runs table: start time, agent, runtime bar, cost, labels and run ID."""
    columns = [
        column
        for column in (
            "started",
            "name",
            "runtime_s",
            "estimated_cost_usd",
            "cost_status",
            "labels",
            "run_id",
        )
        if column in df and (column != "labels" or df["labels"].astype(bool).any())
    ]
    st.dataframe(
        df[columns],
        width="stretch",
        hide_index=True,
        height=min(38 + 35 * len(df), max_height),
        column_config={
            "started": st.column_config.DatetimeColumn("Started", format="MMM D, HH:mm:ss"),
            "name": st.column_config.TextColumn("Agent"),
            "runtime_s": st.column_config.ProgressColumn(
                "Runtime",
                format="%.2f s",
                min_value=0.0,
                max_value=max(float(df["runtime_s"].max() or 0), 0.001),
            ),
            "estimated_cost_usd": st.column_config.NumberColumn("Cost", format="$%.4f"),
            "cost_status": st.column_config.TextColumn("Cost status"),
            "labels": st.column_config.TextColumn("Labels"),
            "run_id": st.column_config.TextColumn("Run ID"),
        },
    )


def render_waterfall(events: pd.DataFrame) -> None:
    """When each event ran, relative to the trace's first event."""
    if events.empty or "started_at" not in events or "duration_ms" not in events:
        return
    started = pd.to_datetime(events["started_at"], errors="coerce", utc=True)
    if started.isna().all():
        return
    origin = started.min()
    chart_df = pd.DataFrame(
        {
            "step": [f"{index + 1}. {name}" for index, name in enumerate(events["name"])],
            "type": events["event_type"].astype(str).str.replace("_", " "),
            "start_ms": (started - origin).dt.total_seconds() * 1000,
            "duration_ms": pd.to_numeric(events["duration_ms"], errors="coerce").fillna(0),
            "status": events.get("status", pd.Series([None] * len(events))),
        }
    ).dropna(subset=["start_ms"])
    chart_df["end_ms"] = chart_df["start_ms"] + chart_df["duration_ms"]
    st.altair_chart(
        alt.Chart(chart_df)
        .mark_bar(cornerRadius=3, height=14)
        .encode(
            x=alt.X("start_ms:Q", title="Milliseconds from start"),
            x2="end_ms:Q",
            y=alt.Y(
                "step:N",
                title=None,
                sort=list(chart_df["step"]),
                axis=alt.Axis(labelLimit=220),
            ),
            color=alt.Color("type:N", title="Event", legend=alt.Legend(orient="bottom")),
            tooltip=[
                alt.Tooltip("step:N", title="Step"),
                alt.Tooltip("type:N", title="Event"),
                alt.Tooltip("start_ms:Q", title="Starts at (ms)", format=",.1f"),
                alt.Tooltip("duration_ms:Q", title="Duration (ms)", format=",.1f"),
                alt.Tooltip("status:N", title="Status"),
            ],
        )
        .properties(height=min(60 + 26 * len(chart_df), 520)),
        width="stretch",
    )


def trace_options(traces: list[dict]) -> dict[str, str]:
    return {f"{t['name']} - {t['run_id']}": t["run_id"] for t in traces}


def pick_trace(traces: list[dict], label: str, index: int = 0, key: str | None = None) -> str:
    """Run ID chosen from the newest runs, or typed in to reach an older run."""
    options = trace_options(traces)
    labels = list(options)
    chosen = options[st.selectbox(label, labels, index=min(index, len(labels) - 1), key=key)]
    if len(traces) >= RECENT_TRACE_LIMIT:
        typed = st.text_input(
            f"{label}: older run ID",
            key=f"{key or label}_older_run_id",
            help=f"Only the newest {RECENT_TRACE_LIMIT} runs are listed. "
            "Enter a run ID from the Traces page to open an older run.",
        ).strip()
        if typed:
            chosen = typed
    return chosen


def select_trace(traces: list[dict], label: str = "Choose trace") -> AgentTrace | None:
    run_id = pick_trace(traces, label)
    trace = load_trace_for_project(run_id, project_id)
    if trace is None:
        st.warning(f"No run `{run_id}` is stored for this project.")
    return trace


def render_gate_table(results: list[dict]) -> None:
    rows = [
        {
            "gate": item["name"],
            "status": "pass" if item["passed"] else "fail",
            "detail": item["detail"],
        }
        for item in results
    ]
    st.dataframe(
        pd.DataFrame(rows),
        width="stretch",
        hide_index=True,
        column_config={
            "gate": st.column_config.TextColumn("Gate"),
            "status": st.column_config.TextColumn("Status", width="small"),
            "detail": st.column_config.TextColumn("Detail", width="large"),
        },
    )


def render_comparison(baseline: AgentTrace | None, candidate: AgentTrace | None, key: str) -> None:
    """Gate inputs, replay proof and PR comment for one baseline/candidate pair."""
    st.markdown("#### Gates")
    g1, g2, g3, g4 = st.columns(4, vertical_alignment="bottom")
    min_latency = g1.number_input(
        "Min latency improvement %", min_value=0.0, value=20.0, key=f"{key}_latency"
    )
    min_cost = g2.number_input(
        "Min cost improvement %", min_value=0.0, value=5.0, key=f"{key}_cost"
    )
    require_schema = g3.checkbox("Require schema valid", value=False, key=f"{key}_schema")
    min_quality = g4.number_input(
        "Min quality score", min_value=0.0, max_value=1.0, value=0.0, key=f"{key}_quality"
    )
    quality_threshold = min_quality if min_quality > 0 else None

    if baseline is not None and candidate is not None:
        gates = ReplayGates(
            min_latency_improvement_pct=min_latency,
            min_cost_improvement_pct=min_cost,
            require_schema_valid=require_schema,
            min_quality_score=quality_threshold,
        )
        replay = build_replay_report(baseline, candidate, gates=gates)
        ci_report = build_ci_report(baseline, candidate, gates=gates)
        status = "passed" if replay["gates"]["passed"] else "failed"
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Status", status)
        c2.metric("Latency improvement", f"{replay['deltas']['latency_improvement_pct']:.1f}%")
        cost_improvement = replay["deltas"]["cost_improvement_pct"]
        c3.metric("Cost improvement", percentage(cost_improvement))
        c4.metric("Retry delta", replay["deltas"]["retry_count_delta"])

        st.subheader("Gate results")
        render_gate_table(replay["gates"]["results"])

        st.subheader("Metric deltas")
        metric_rows = [
            {"metric": name, "value": value}
            for name, value in replay["deltas"].items()
            if name.endswith("_delta") or name.endswith("_pct")
        ]
        st.dataframe(pd.DataFrame(metric_rows), width="stretch", hide_index=True)

        st.subheader("PR comment preview")
        st.code(
            "Trace in. Rewrite plan out. Replay proof in the PR.\n\n"
            + ci_report_to_markdown(ci_report)
        )

        col_json, col_md = st.columns(2)
        col_json.download_button(
            "Download replay JSON",
            data=json.dumps(replay, indent=2),
            file_name="agentloop_replay_report.json",
            mime="application/json",
            key=f"{key}_json",
        )
        col_md.download_button(
            "Download PR Markdown",
            data=ci_report_to_markdown(ci_report),
            file_name="agentloop_ci.md",
            mime="text/markdown",
            key=f"{key}_markdown",
        )


store = load_store()
RECENT_TRACE_LIMIT = MAX_PAGE_SIZE
project_mode = require_project_key()

if project_mode:
    # A shared dashboard shows exactly one project: the one the API key belongs to.
    if "signed_in_project" in st.session_state and not session_valid(store, st.session_state):
        st.session_state["signin_notice"] = (
            "Your project API key was revoked or is no longer valid. Sign in again."
        )
    if "signed_in_project" not in st.session_state:
        _, center, _ = st.columns([1, 2, 1])
        with center:
            page_header(
                "Sign in",
                description="Enter a project API key. The dashboard shows only the traces "
                "and findings of the project that key belongs to.",
            )
            if notice := st.session_state.pop("signin_notice", None):
                st.warning(notice)
            with st.form("project_sign_in"):
                entered_key = st.text_input("Project API key", type="password")
                submitted = st.form_submit_button(
                    "Sign in", type="primary", icon=":material/login:", width="stretch"
                )
            if submitted:
                if sign_in(store, entered_key, st.session_state) is not None:
                    st.rerun()
                remaining = lockout_remaining(st.session_state)
                if remaining > 0:
                    st.error(
                        f"Too many failed attempts. Try again in {int(remaining) + 1} seconds."
                    )
                else:
                    st.error("That API key is not valid for any project.")
        st.stop()
    project_id = st.session_state["signed_in_project"]
    st.sidebar.markdown(f"**Project:** `{project_id}`")
    if st.sidebar.button("Sign out"):
        sign_out(st.session_state)
        st.rerun()
else:
    project_id = st.sidebar.text_input(
        "Project", value=st.session_state.get("project_id", "default")
    )
    st.session_state["project_id"] = project_id

# Recorded in the store's audit log for keys created and traces deleted here.
dashboard_actor = (
    f"dashboard:api-key:{st.session_state.get('signed_in_key_prefix', '')}"
    if project_mode
    else "dashboard"
)

page = st.sidebar.radio(
    "Navigate",
    visible_pages(PAGE_NAMES, project_mode=project_mode),
    format_func=nav_label,
    label_visibility="collapsed",
)

page_header(page, project_id)

if page == "Overview":
    summary = store.usage_summary(project_id=project_id)
    traces = recent_traces()

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

elif page == "Traces":
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
                    st.session_state[cursor_key] = [None]
                    st.session_state["trace_deleted_notice"] = (
                        f"Deleted {trace.run_id} and {deleted['findings']} findings, "
                        f"{deleted['interventions']} interventions."
                        if deleted
                        else f"{trace.run_id} was already deleted."
                    )
                    st.rerun()

elif page == "Optimization Queue":
    dimension = st.selectbox(
        "Sort investigations by", SORT_FIELDS, format_func=lambda value: value.replace("_", " ")
    )
    queue = sort_ranked(store.optimization_queue(project_id=project_id), dimension)
    finding_counts = store.finding_counts(project_id=project_id)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Open opportunities", len(queue))
    c2.metric("Persisted findings", finding_counts["total"])
    c3.metric("Patchable findings", finding_counts["patchable"])
    c4.metric(
        "Estimated latency savings",
        seconds(
            None
            if any(item["estimated_latency_savings_ms"] is None for item in queue)
            else sum(item["estimated_latency_savings_ms"] for item in queue)
        ),
    )

    if not queue:
        st.info(
            "No optimization opportunities have been persisted yet. Store traces or run diagnosis first."
        )
    else:
        queue_df = pd.DataFrame(queue)
        queue_df["readiness"] = [item["ranking"]["status"].replace("_", " ") for item in queue]
        visible_cols = [
            "priority_score",
            "readiness",
            "severity",
            "type",
            "title",
            "occurrence_count",
            "run_count",
            "patchable_count",
            "quality_risk",
            "requires_scorer",
            "safe_to_auto_patch",
            "estimated_latency_savings_ms",
            "estimated_cost_savings_usd",
        ]
        st.dataframe(
            queue_df[visible_cols],
            width="stretch",
            hide_index=True,
            column_config={
                "priority_score": st.column_config.NumberColumn("Priority", width="small"),
                "readiness": st.column_config.TextColumn("Readiness"),
                "severity": st.column_config.TextColumn("Severity", width="small"),
                "type": st.column_config.TextColumn("Type"),
                "title": st.column_config.TextColumn("Finding", width="large"),
                "occurrence_count": st.column_config.NumberColumn("Occurrences"),
                "run_count": st.column_config.NumberColumn("Runs"),
                "patchable_count": st.column_config.NumberColumn("Patchable"),
                "quality_risk": st.column_config.TextColumn("Quality risk"),
                "requires_scorer": st.column_config.CheckboxColumn("Needs scorer"),
                "safe_to_auto_patch": st.column_config.CheckboxColumn("Auto-patch safe"),
                "estimated_latency_savings_ms": st.column_config.NumberColumn(
                    "Latency savings", format="%.0f ms"
                ),
                "estimated_cost_savings_usd": st.column_config.NumberColumn(
                    "Cost savings", format="$%.4f"
                ),
            },
        )

        st.divider()
        selected_title = st.selectbox("Open queue item", queue_df["title"].tolist())
        item = next(row for row in queue if row["title"] == selected_title)
        st.subheader(item["title"])
        readiness = item["ranking"]["status"].replace("_", " ")
        with st.container(horizontal=True, gap="small"):
            st.badge(
                f"{item['severity']} severity",
                color=SEVERITY_COLORS.get(item["severity"], "gray"),
            )
            st.badge(
                readiness,
                icon=":material/check_circle:" if readiness == "ready to test" else None,
                color="green" if readiness == "ready to test" else "orange",
            )
            st.badge(item["type"], color="gray")
        c5, c6, c7, c8 = st.columns(4)
        c5.metric("Investigation rank", item["ranking"]["priority_rank"])
        c6.metric("Affected runs", item["run_count"])
        c7.metric("Latency savings", seconds(item["estimated_latency_savings_ms"]))
        c8.metric("Cost savings", money(item["estimated_cost_savings_usd"]))
        st.dataframe(
            pd.DataFrame(
                [
                    ("Status", item["status"]),
                    ("Test readiness", readiness),
                    ("Quality risk", item["quality_risk"]),
                    ("Patchable occurrences", str(item["patchable_count"])),
                    ("Requires scorer", "yes" if item["requires_scorer"] else "no"),
                    ("Safe to auto-patch", "yes" if item["safe_to_auto_patch"] else "no"),
                ],
                columns=["Field", "Value"],
            ),
            hide_index=True,
            width="stretch",
        )
        with st.expander(f"Affected runs ({len(item['affected_runs'])})"):
            st.code("\n".join(item["affected_runs"]), language="text")
        with st.expander("Ranking inputs and provenance"):
            st.json(item["ranking"])

        item_findings = store.list_findings(
            project_id=project_id, type=item["type"], title=item["title"]
        )
        if item_findings:
            st.subheader("Finding instances")
            st.dataframe(pd.DataFrame(item_findings), width="stretch", hide_index=True)

            st.markdown("**Update finding status**")
            finding_labels = {
                f"{f['run_id']} · {f['finding_id']} (currently {f['status']})": f
                for f in item_findings
            }
            selected_finding_label = st.selectbox(
                "Finding", list(finding_labels.keys()), key=f"finding_select_{item['queue_id']}"
            )
            selected_finding = finding_labels[selected_finding_label]
            allowed_targets = sorted(
                ALLOWED_FINDING_TRANSITIONS.get(selected_finding["status"], ())
            )
            if allowed_targets:
                new_status = st.selectbox(
                    "New status", allowed_targets, key=f"status_select_{item['queue_id']}"
                )
                if st.button("Apply transition", key=f"apply_transition_{item['queue_id']}"):
                    try:
                        store.update_finding_status(
                            project_id,
                            selected_finding["run_id"],
                            selected_finding["finding_id"],
                            new_status,
                        )
                    except (FindingNotFoundError, FindingTransitionError) as exc:
                        st.error(str(exc))
                    else:
                        st.success(f"{selected_finding['finding_id']} is now {new_status}")
                        st.rerun()
            else:
                st.caption(f"No transitions available from '{selected_finding['status']}'.")

        st.download_button(
            "Download optimization queue JSON",
            data=json.dumps({"project_id": project_id, "queue": queue}, indent=2),
            file_name="agentloop_optimization_queue.json",
            mime="application/json",
        )

        st.subheader("GitHub issue drafts")
        drafts = build_issue_drafts(queue)
        if not drafts:
            st.info("No patchable queue items available for issue drafts.")
        else:
            selected_issue = st.selectbox(
                "Preview issue draft", [draft["title"] for draft in drafts]
            )
            draft = next(item for item in drafts if item["title"] == selected_issue)
            st.write(f"Labels: `{', '.join(draft['labels'])}`")
            st.code(draft["body"], language="markdown")
            st.download_button(
                "Download issue drafts Markdown",
                data=issue_drafts_to_markdown(drafts),
                file_name="agentloop_issue_drafts.md",
                mime="text/markdown",
            )

elif page == "Optimization":
    traces = recent_traces()
    if not traces:
        st.info("No traces stored for this project yet.")
    else:
        trace = select_trace(traces)
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

elif page == "Diagnosis":
    traces = recent_traces()
    if not traces:
        st.info("No traces stored for this project yet.")
    else:
        trace = select_trace(traces, "Choose trace for diagnosis")
        if trace is not None:
            dimension = st.selectbox(
                "Sort findings by", SORT_FIELDS, format_func=lambda value: value.replace("_", " ")
            )
            diagnosis = build_diagnosis(trace, sort_by=dimension)
            if diagnosis.get("rule_errors"):
                st.warning(
                    "Analysis incomplete; failed rules: "
                    + ", ".join(error["rule_id"] for error in diagnosis["rule_errors"])
                )
            summary = diagnosis["summary"]
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Findings", summary["finding_count"])
            c2.metric("High severity", summary["high_severity_count"])
            c3.metric("Patchable", summary["patchable_count"])
            c4.metric("Latency reduction", f"{summary['estimated_latency_reduction_pct']:.1f}%")

            findings = diagnosis.get("findings", [])
            if not findings:
                st.info("No machine-actionable findings detected for this trace.")
            for finding in findings:
                label = f"{finding['severity'].upper()} - {finding['title']} ({finding['type']})"
                with st.expander(label, expanded=finding["severity"] == "high"):
                    st.write(finding["metadata"].get("why", ""))
                    render_estimate_details(finding)
                    st.write("Test readiness: " + finding["ranking"]["status"].replace("_", " "))
                    st.json(finding["ranking"])
                    st.write(f"Finding ID: `{finding['finding_id']}`")
                    st.write(f"Confidence: `{finding['confidence']}`")
                    st.write(f"Affected spans: `{', '.join(finding['affected_spans'])}`")
                    st.write(f"Rewrite: {finding['rewrite']['hint']}")
                    st.write(f"Validation: {finding['validation']['acceptance_criteria']}")
                    st.write(
                        "Estimated savings: "
                        f"{seconds(finding['savings']['estimated_latency_savings_ms'])}, "
                        f"{money(finding['savings']['estimated_cost_savings_usd'])}"
                    )
                    if finding["evidence"]:
                        st.dataframe(
                            pd.DataFrame(finding["evidence"]), width="stretch", hide_index=True
                        )

            st.download_button(
                "Download diagnosis JSON",
                data=json.dumps(diagnosis, indent=2),
                file_name="agentloop_diagnosis.json",
                mime="application/json",
            )

elif page == "Patch Plan":
    traces = recent_traces()
    if not traces:
        st.info("No traces stored for this project yet.")
    else:
        trace = select_trace(traces, "Choose trace for patch planning")
        repo_path = st.text_input("Repository path", value=str(Path.cwd()))
        if trace is not None:
            try:
                plan = build_patch_plan(trace, repo_path=repo_path)
            except RepositoryPathError as exc:
                # The path is free text re-read on every rerun, so a half-typed one
                # reaches build_patch_plan before the operator has finished editing.
                # Only the path error is caught: anything else planning raises is a
                # bug in planning, and swallowing it here would hide it.
                st.error(f"Fix the repository path: {exc}.")
                st.stop()
            summary = plan["summary"]
            c1, c2, c3 = st.columns(3)
            c1.metric("Patch plans", summary["patch_count"])
            c2.metric("Unsupported", summary["unsupported_finding_count"])
            frameworks = summary["frameworks_detected"]
            c3.metric("Frameworks detected", len(frameworks))
            if frameworks:
                with st.container(horizontal=True, gap="small"):
                    for framework in frameworks:
                        st.badge(framework, icon=":material/extension:", color="violet")

            for item in plan.get("patch_plans", []):
                with st.expander(f"{item['patch_id']} - {item['title']}", expanded=True):
                    st.write(f"Type: `{item['type']}`")
                    st.write(f"Risk: `{item['risk']}`")
                    st.write(f"Framework: `{item['framework']}`")
                    st.write(f"Finding: `{item['finding_id']}`")
                    st.write(f"Before pattern: {item['before_pattern']}")
                    st.write(f"Proposed rewrite: {item['proposed_rewrite']}")
                    st.code(item["suggested_diff"], language="text")
                    st.write(f"Validation command: `{item['validation_command']}`")
                    st.write(f"Acceptance: {item['acceptance_criteria']}")
                    if item["files"]:
                        st.dataframe(pd.DataFrame(item["files"]), width="stretch", hide_index=True)
                    if item["notes"]:
                        for note in item["notes"]:
                            st.write(f"- {note}")

            if plan.get("unsupported_findings"):
                st.subheader("Unsupported findings")
                st.dataframe(
                    pd.DataFrame(plan["unsupported_findings"]), width="stretch", hide_index=True
                )

            st.download_button(
                "Download patch plan JSON",
                data=json.dumps(plan, indent=2),
                file_name="agentloop_patch_plan.json",
                mime="application/json",
            )

elif page == "CI Runs":
    st.caption(
        "Baseline and candidate traces uploaded by the reusable comparison workflow, grouped "
        "by their workflow_run label. Choose a run to see its comparison."
    )
    runs = list_ci_runs(store, project_id)
    if not runs:
        st.info(
            "No traces with a workflow_run label in this project yet. Upload them from CI with "
            "agentloop_api_url on the reusable comparison workflow."
        )
    else:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Workflow run": run["workflow_run"],
                        "Commit": (run["commit"] or "")[:12],
                        "Ref": run["ref"],
                        "Repository": run["repository"],
                        "Baseline": run["baseline_run_id"],
                        "Candidate": run["candidate_run_id"],
                        "Latest upload": run["latest_upload"],
                    }
                    for run in runs
                ]
            ),
            width="stretch",
            hide_index=True,
        )
        comparable = [run for run in runs if run["comparable"]]
        if not comparable:
            st.warning("No run has both a baseline and a candidate trace yet.")
        else:
            labels = {
                f"{run['workflow_run']} · {(run['commit'] or 'unknown commit')[:12]} · "
                f"{run['ref'] or 'unknown ref'}": run["workflow_run"]
                for run in comparable
            }
            chosen = labels[st.selectbox("Compare run", list(labels))]
            try:
                baseline, candidate = ci_run_pair(store, project_id, chosen)
            except CIRunNotFoundError as exc:
                st.error(str(exc))
            else:
                st.caption(f"Baseline {baseline.run_id} · candidate {candidate.run_id}")
                render_comparison(baseline, candidate, key="ci_run")

elif page == "Replay Proof":
    traces = recent_traces()
    if len(traces) < 2:
        st.info("Store at least two traces to compare a baseline and candidate.")
    else:
        baseline = load_trace_for_project(pick_trace(traces, "Baseline trace"), project_id)
        candidate = load_trace_for_project(
            pick_trace(traces, "Candidate trace", index=1), project_id
        )

        render_comparison(baseline, candidate, key="replay")

elif page == "Quality Gates":
    traces = recent_traces()
    st.caption(
        "Compare caller-defined quality criteria alongside recorded cost and latency. "
        "Legacy scorers and versioned structured decision, multilabel, numeric, extraction and matching scorers are supported. Custom Python scorers are trusted local code."
    )
    if len(traces) < 2:
        st.info("Store at least two traces to score baseline and candidate quality.")
    else:
        default_fixture = [
            {
                "id": "required_summary_fields",
                "input": "Summarize one source.",
                "candidate_output": {
                    "summary": "AgentLoop proves rewrites.",
                    "sources": ["source-a"],
                },
                "baseline_output": {
                    "summary": "AgentLoop proves rewrites.",
                    "sources": ["source-a"],
                },
                "scorer": {"type": "required_fields", "required": ["summary", "sources"]},
            }
        ]
        with st.form("quality_gate_form"):
            baseline_run_id = pick_trace(traces, "Baseline trace", key="quality_baseline")
            candidate_run_id = pick_trace(
                traces, "Candidate trace", index=1, key="quality_candidate"
            )
            min_score = st.number_input(
                "Minimum candidate quality score", min_value=0.0, max_value=1.0, value=0.9
            )
            uploaded = st.file_uploader(
                "Upload quality fixture JSON", type=["json"], key="quality_fixture_upload"
            )
            fixtures_text = st.text_area(
                "Fixture JSON",
                value=json.dumps({"fixtures": default_fixture}, indent=2),
                height=260,
            )
            submitted = st.form_submit_button("Run quality gate")

        if submitted:
            try:
                raw_fixtures = (
                    uploaded.read().decode("utf-8") if uploaded is not None else fixtures_text
                )
                fixtures = parse_quality_fixtures(json.loads(raw_fixtures))
                baseline = load_trace_for_project(baseline_run_id, project_id)
                candidate = load_trace_for_project(candidate_run_id, project_id)
                if baseline is None or candidate is None:
                    raise QualityValidationError(
                        "selected baseline or candidate trace is unavailable"
                    )
                quality = build_quality_report(
                    fixtures,
                    baseline_trace=baseline,
                    candidate_trace=candidate,
                    min_score=min_score,
                )
            except json.JSONDecodeError as exc:
                st.error(
                    "Fix the quality fixture JSON: "
                    f"{exc.msg} at line {exc.lineno}, column {exc.colno}."
                )
            except (UnicodeError, QualityValidationError) as exc:
                st.error(f"Fix the quality fixture JSON: {exc}.")
            else:
                status = "passed" if quality["passed"] else "failed"
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Status", status)
                c2.metric(
                    "Candidate score",
                    "unavailable"
                    if quality["candidate_score"] is None
                    else str(quality["candidate_score"])
                    if quality.get("schema_version") == "2.0"
                    else f"{quality['candidate_score']:.4f}",
                )
                c3.metric(
                    "Quality delta",
                    "unavailable"
                    if quality["quality_delta"] is None
                    else str(quality["quality_delta"])
                    if quality.get("schema_version") == "2.0"
                    else f"{quality['quality_delta']:.4f}",
                )
                c4.metric("Failed cases", quality["failed_case_count"])
                if quality.get("schema_version") == "2.0":
                    st.caption(
                        f"Indeterminate cases: {quality['indeterminate_case_count']}; incomplete evidence cannot pass the quality gate."
                    )
                    with st.expander("Quality denominators and classification metrics"):
                        st.json(
                            {
                                "summaries": quality["summaries"],
                                "classification": quality["classification"],
                            }
                        )
                st.dataframe(pd.DataFrame(quality["cases"]), width="stretch", hide_index=True)
                st.download_button(
                    "Download quality report JSON",
                    data=json.dumps(quality, indent=2),
                    file_name="agentloop_quality_report.json",
                    mime="application/json",
                )
                st.download_button(
                    "Download quality report Markdown",
                    data=quality_report_to_markdown(quality),
                    file_name="agentloop_quality_report.md",
                    mime="text/markdown",
                )

elif page == "Value & Pricing":
    traces = recent_traces()
    if not traces:
        st.info(
            "No traces stored for this project yet. Generate demo traces from the Ingest page first."
        )
    else:
        trace = select_trace(traces, label="Choose trace for value report")
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

elif page == "Audit Log":
    limit_col, note_col = st.columns([1, 3], vertical_alignment="bottom")
    limit = limit_col.number_input(
        "Events to show", min_value=10, max_value=500, value=100, step=10
    )
    note_col.caption("Key values are never recorded.")
    events = store.list_audit_events(project_id, limit=int(limit))
    if not events:
        st.info("No audit events for this project yet.")
    else:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "When": event["occurred_at"],
                        "Action": event["action"],
                        "Actor": event["actor"],
                        "Target": event["target"],
                        "Details": json.dumps(event["details"], sort_keys=True),
                    }
                    for event in events
                ]
            ),
            width="stretch",
            hide_index=True,
        )

elif page == "API Keys":
    with st.container(border=True):
        st.markdown("#### Create a project API key")
        st.caption(
            f"The key can upload and read traces in project `{project_id}`. It is shown "
            "only once, so copy it before leaving this page."
        )
        name_col, button_col = st.columns([3, 1], vertical_alignment="bottom")
        key_name = name_col.text_input("Key name", value="local-dev")
        create = button_col.button(
            "Create API key", type="primary", icon=":material/key:", width="stretch"
        )
        if create:
            created = store.create_api_key(
                project_id=project_id, name=key_name, actor=dashboard_actor
            )
            st.success(f"Created key for project `{project_id}`", icon=":material/check_circle:")
            st.code(created["api_key"])

    st.subheader("Use this key")
    st.code(
        "uv run agentloop upload --path runs/research_agent_baseline.json "
        "--api-url http://127.0.0.1:8000 --api-key YOUR_KEY",
        language="bash",
    )

elif page == "Ingest":
    # Demo generators write files into the dashboard host's shared runs directory,
    # so a shared dashboard offers only uploads into the signed-in project.
    notice = None
    if project_mode:
        upload_card = st.container(border=True)
    else:
        generate_card, upload_card = st.columns(2, gap="medium", border=True)
        with generate_card:
            st.markdown("#### Generate demo traces")
            st.caption(f"Runs a demo agent and stores its trace in project `{project_id}`.")
            generated: list[Path] = []
            first_row = st.columns(2, gap="small")
            if first_row[0].button("Generate baseline", width="stretch"):
                generated = [run_baseline(runs_dir)]
            if first_row[1].button("Generate optimized", width="stretch"):
                generated = [run_optimized(runs_dir)]
            second_row = st.columns(2, gap="small")
            if second_row[0].button("Generate LangGraph-style", width="stretch"):
                generated = [run_langgraph_style(runs_dir)]
            if second_row[1].button("Generate proof demo pair", width="stretch"):
                generated = list(run_proof_pair(runs_dir))
            if st.button("Generate all", type="primary", icon=":material/bolt:", width="stretch"):
                generated = [
                    run_baseline(runs_dir),
                    run_optimized(runs_dir),
                    run_langgraph_style(runs_dir),
                    *run_proof_pair(runs_dir),
                ]
            stored = []
            for path in generated:
                trace = AgentTrace.from_json(path)
                store.save_trace(trace, project_id=project_id)
                stored.append(trace)
            if len(stored) == 1:
                notice = f"Generated and stored `{stored[0].name}` as `{stored[0].run_id}`."
            elif stored:
                notice = f"Generated and stored {len(stored)} demo traces in `{project_id}`."

    with upload_card:
        # Demo generators write files into the dashboard host's shared runs
        # directory, so a shared dashboard offers only uploads into the
        # signed-in project.
        st.markdown("#### Upload a trace")
        st.caption(f"Store an AgentLoop trace JSON file in project `{project_id}`.")
        uploaded = st.file_uploader(
            "Upload AgentLoop trace JSON", type=["json"], label_visibility="collapsed"
        )
        if uploaded is not None:
            try:
                trace = parse_uploaded_trace(uploaded.read())
            except TraceUploadError as exc:
                st.error(f"Fix the uploaded trace: {exc}.")
            else:
                st.caption(f"`{trace.name}` · `{trace.run_id}` · {len(trace.events)} events")
                if st.button("Store uploaded trace", type="primary", width="stretch"):
                    store.save_trace(trace, project_id=project_id)
                    notice = f"Stored uploaded trace `{trace.run_id}` in `{project_id}`."

    if notice:
        st.success(notice, icon=":material/check_circle:")

    st.subheader("CLI ingest")
    st.code(
        "uv run agentloop demo-all\n"
        f"uv run agentloop store-trace --path runs/research_agent_baseline.json --project-id {project_id}\n"
        f"uv run agentloop list-stored-traces --project-id {project_id}",
        language="bash",
    )

elif page == "Setup":
    st.subheader("Local development")
    st.code(
        "uv sync --locked --all-extras --dev\n"
        "uv run agentloop init-store\n"
        "uv run agentloop demo-all\n"
        f"uv run agentloop store-trace --path runs/research_agent_baseline.json --project-id {project_id}\n"
        "uv run streamlit run dashboard/app.py",
        language="bash",
    )

    st.subheader("Hosted API")
    st.code(
        "uv run agentloop create-api-key --project-id acme --name local-dev\n"
        "uv run agentloop server --host 127.0.0.1 --port 8000\n"
        "uv run agentloop upload --path runs/research_agent_baseline.json "
        "--api-url http://127.0.0.1:8000 --api-key al_xxx\n"
        "uv run agentloop remote-usage --api-url http://127.0.0.1:8000 --api-key al_xxx",
        language="bash",
    )

    st.subheader("Postgres deployment")
    st.code(
        "uv sync --locked --extra server --extra postgres --no-dev\n"
        "export AGENTLOOP_STORE_BACKEND=postgres\n"
        "export AGENTLOOP_DATABASE_URL=postgresql://agentloop:agentloop@localhost:5432/agentloop\n"
        "uv run agentloop init-store\n"
        "uv run agentloop server --host 0.0.0.0 --port 8000",
        language="bash",
    )
