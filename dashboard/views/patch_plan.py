"""The Patch Plan page.

Split out of ``dashboard/app.py`` (issue #407).
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from agentloop.patches import RepositoryPathError, build_patch_plan

try:
    from dashboard.common import recent_traces, select_trace
except ModuleNotFoundError:
    from common import recent_traces, select_trace


def render(ctx) -> None:
    store = ctx.store
    project_id = ctx.project_id
    traces = recent_traces(store, project_id)
    if not traces:
        st.info("No traces stored for this project yet.")
    else:
        trace = select_trace(traces, project_id, "Choose trace for patch planning")
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
