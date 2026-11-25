"""The Quality Gates page.

Split out of ``dashboard/app.py`` (issue #407).
"""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from agentloop.quality import (
    QualityValidationError,
    build_quality_report,
    parse_quality_fixtures,
    quality_report_to_markdown,
)

try:
    from dashboard.common import (
        load_trace_for_project,
        pick_trace,
        recent_traces,
    )
except ModuleNotFoundError:
    from common import (
        load_trace_for_project,
        pick_trace,
        recent_traces,
    )


def render(ctx) -> None:
    store = ctx.store
    project_id = ctx.project_id
    traces = recent_traces(store, project_id)
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
