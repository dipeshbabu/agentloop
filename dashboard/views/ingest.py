"""The Ingest page.

Split out of ``dashboard/app.py`` (issue #407).
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from agentloop.demo import run_baseline, run_langgraph_style, run_optimized, run_proof_pair
from agentloop.tracer import AgentTrace

try:
    from dashboard.trace_upload import TraceUploadError, parse_uploaded_trace
except ModuleNotFoundError:
    from trace_upload import TraceUploadError, parse_uploaded_trace

try:
    from dashboard.common import invalidate_cached_results
except ModuleNotFoundError:
    from common import invalidate_cached_results


def render(ctx) -> None:
    runs_dir = ctx.runs_dir
    store = ctx.store
    project_id = ctx.project_id
    project_mode = ctx.project_mode
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
            if stored:
                invalidate_cached_results()
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
                    invalidate_cached_results()
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
