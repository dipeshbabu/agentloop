"""The CI Runs page.

Split out of ``dashboard/app.py`` (issue #407).
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from agentloop.ci_runs import CIRunNotFoundError, ci_run_pair, list_ci_runs

try:
    from dashboard.common import render_comparison
except ModuleNotFoundError:
    from common import render_comparison


def render(ctx) -> None:
    store = ctx.store
    project_id = ctx.project_id
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
