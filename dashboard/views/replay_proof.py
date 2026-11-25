"""The Replay Proof page.

Split out of ``dashboard/app.py`` (issue #407).
"""

from __future__ import annotations

import streamlit as st

try:
    from dashboard.common import (
        load_trace_for_project,
        pick_trace,
        recent_traces,
        render_comparison,
    )
except ModuleNotFoundError:
    from common import (
        load_trace_for_project,
        pick_trace,
        recent_traces,
        render_comparison,
    )


def render(ctx) -> None:
    store = ctx.store
    project_id = ctx.project_id
    traces = recent_traces(store, project_id)
    if len(traces) < 2:
        st.info("Store at least two traces to compare a baseline and candidate.")
    else:
        baseline = load_trace_for_project(pick_trace(traces, "Baseline trace"), project_id)
        candidate = load_trace_for_project(
            pick_trace(traces, "Candidate trace", index=1), project_id
        )

        render_comparison(baseline, candidate, key="replay")
