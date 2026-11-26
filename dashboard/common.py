"""Data access, caching and rendering helpers shared by dashboard pages.

Split out of ``dashboard/app.py`` (issue #407).
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from agentloop.ci import build_ci_report, ci_report_to_markdown
from agentloop.replay import ReplayGates, build_replay_report
from agentloop.store import (
    MAX_PAGE_SIZE,
    get_store,
)
from agentloop.tracer import AgentTrace

try:
    from dashboard.value_view import percentage
except ModuleNotFoundError:
    from value_view import percentage


def runs_directory() -> Path:
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


@st.cache_resource
def load_store():
    db = get_store()
    db.init()
    return db


# Streamlit reruns the page on every interaction, and building the queue reads
# and ranks up to 5,000 findings (about a second at 2,000 traces). Results are
# reused for this long, and every write made from the dashboard clears them.
QUEUE_CACHE_SECONDS = 30


@st.cache_resource
def _store_cache_token() -> str:
    """Changes whenever load_store()'s store is replaced, so cached results never cross stores."""
    return uuid.uuid4().hex


@st.cache_data(ttl=QUEUE_CACHE_SECONDS, max_entries=64, show_spinner=False)
def cached_optimization_queue(store_token: str, project_id: str) -> tuple[list[dict], str]:
    """The project's optimization queue and the UTC time it was computed."""
    queue = load_store().optimization_queue(project_id=project_id)
    return queue, datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


def invalidate_cached_results() -> None:
    """Drop cached results after the dashboard changes traces or findings."""
    cached_optimization_queue.clear()


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


def recent_traces(store, project_id: str) -> list[dict]:
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


def select_trace(
    traces: list[dict], project_id: str, label: str = "Choose trace"
) -> AgentTrace | None:
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


# Only this many runs are listed; older runs are opened by ID.
RECENT_TRACE_LIMIT = MAX_PAGE_SIZE
