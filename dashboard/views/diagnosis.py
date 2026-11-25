"""The Diagnosis page.

Split out of ``dashboard/app.py`` (issue #407).
"""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from agentloop.findings import build_diagnosis
from agentloop.ranking import SORT_FIELDS

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
        trace = select_trace(traces, project_id, "Choose trace for diagnosis")
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
