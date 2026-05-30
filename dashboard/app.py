from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from agentloop.ci import build_ci_report, ci_report_to_markdown
from agentloop.demo import run_baseline, run_langgraph_style, run_optimized, run_proof_pair
from agentloop.findings import build_diagnosis
from agentloop.issues import build_issue_drafts, issue_drafts_to_markdown
from agentloop.optimizer import build_optimization_plan
from agentloop.patches import build_patch_plan
from agentloop.replay import ReplayGates, build_replay_report
from agentloop.store import get_store
from agentloop.tracer import AgentTrace
from agentloop.value import build_value_report

try:
    from dashboard.value_view import assumption_inputs, render_value_report
except ModuleNotFoundError:
    from value_view import assumption_inputs, render_value_report

st.set_page_config(page_title="AgentLoop Cloud", layout="wide")

runs_dir = Path("runs")
runs_dir.mkdir(parents=True, exist_ok=True)


@st.cache_resource
def load_store():
    db = get_store()
    db.init()
    return db


def money(value: float) -> str:
    return f"${float(value):,.4f}"


def seconds(ms: float) -> str:
    return f"{float(ms) / 1000:,.2f}s"


def load_trace_for_project(run_id: str, project_id: str) -> AgentTrace | None:
    return load_store().get_trace(run_id=run_id, project_id=project_id)


def select_trace(traces: list[dict], label: str = "Choose trace") -> AgentTrace | None:
    labels = {f"{t['name']} · {t['run_id']}": t["run_id"] for t in traces}
    selected_label = st.selectbox(label, list(labels.keys()))
    return load_trace_for_project(labels[selected_label], project_id)


def trace_options(traces: list[dict]) -> dict[str, str]:
    return {f"{t['name']} - {t['run_id']}": t["run_id"] for t in traces}


def selected_trace_from_options(traces: list[dict], label: str) -> AgentTrace | None:
    options = trace_options(traces)
    selected = st.selectbox(label, list(options.keys()))
    return load_trace_for_project(options[selected], project_id)


def render_gate_table(results: list[dict]) -> None:
    rows = [
        {
            "gate": item["name"],
            "status": "pass" if item["passed"] else "fail",
            "detail": item["detail"],
        }
        for item in results
    ]
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


st.sidebar.title("AgentLoop Cloud")
st.sidebar.caption("Hosted control panel for agent-loop performance")

project_id = st.sidebar.text_input("Project", value=st.session_state.get("project_id", "default"))
st.session_state["project_id"] = project_id

page = st.sidebar.radio(
    "Navigate",
    [
        "Overview",
        "Traces",
        "Optimization Queue",
        "Optimization",
        "Diagnosis",
        "Patch Plan",
        "Replay Proof",
        "Value & Pricing",
        "API Keys",
        "Ingest",
        "Setup",
    ],
)

store = load_store()

st.title("AgentLoop Cloud")
st.caption("Cost, latency, and execution-graph optimization for production AI agents")

if page == "Overview":
    summary = store.usage_summary(project_id=project_id)
    traces = store.list_traces(project_id=project_id)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Runs", int(summary.get("run_count", 0)))
    c2.metric("Total runtime", seconds(summary.get("total_runtime_ms", 0)))
    c3.metric("Estimated LLM cost", money(summary.get("estimated_cost_usd", 0)))
    c4.metric("Retries", int(summary.get("retry_count", 0)))

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Input tokens", int(summary.get("input_tokens", 0)))
    c6.metric("Output tokens", int(summary.get("output_tokens", 0)))
    c7.metric("Model calls", int(summary.get("model_call_count", 0)))
    c8.metric("Tool calls", int(summary.get("tool_call_count", 0)))

    st.subheader("Recent runs")
    if traces:
        df = pd.DataFrame(traces)
        st.dataframe(df, width="stretch")

        chart_df = df[["name", "total_runtime_ms", "estimated_cost_usd"]].copy()
        chart_df["runtime_s"] = chart_df["total_runtime_ms"] / 1000
        st.subheader("Runtime by run")
        st.bar_chart(chart_df, x="name", y="runtime_s")
    else:
        st.info("No stored traces yet. Use the Ingest page or run `agentloop store-trace`.")

elif page == "Traces":
    st.subheader("Stored traces")
    traces = store.list_traces(project_id=project_id)
    if not traces:
        st.info("No traces stored for this project yet.")
    else:
        df = pd.DataFrame(traces)
        st.dataframe(df, width="stretch")
        selected_run = st.selectbox("Open run", df["run_id"].tolist())
        trace = load_trace_for_project(selected_run, project_id)
        if trace is not None:
            report = trace.report()
            st.markdown(f"### {trace.name}")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Runtime", seconds(report["total_runtime_ms"]))
            c2.metric("Cost", money(report["estimated_cost_usd"]))
            c3.metric("Model calls", report["model_call_count"])
            c4.metric("Tool calls", report["tool_call_count"])

            events = pd.DataFrame(report["events"])
            st.subheader("Event timeline")
            if not events.empty:
                cols = ["event_type", "name", "duration_ms", "model", "input_tokens", "output_tokens", "status"]
                for col in cols:
                    if col not in events.columns:
                        events[col] = None
                st.dataframe(events[cols], width="stretch")

            st.download_button(
                "Download trace JSON",
                data=json.dumps(trace.to_dict(), indent=2),
                file_name=f"{trace.run_id}.json",
                mime="application/json",
            )

elif page == "Optimization Queue":
    st.subheader("Optimization queue")
    queue = store.optimization_queue(project_id=project_id)
    findings = store.list_findings(project_id=project_id)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Open opportunities", len(queue))
    c2.metric("Persisted findings", len(findings))
    c3.metric("Patchable findings", sum(1 for finding in findings if finding["patchable"]))
    c4.metric(
        "Estimated latency savings",
        seconds(sum(item["estimated_latency_savings_ms"] for item in queue)),
    )

    if not queue:
        st.info("No optimization opportunities have been persisted yet. Store traces or run diagnosis first.")
    else:
        queue_df = pd.DataFrame(queue)
        visible_cols = [
            "priority_score",
            "severity",
            "type",
            "title",
            "occurrence_count",
            "run_count",
            "patchable_count",
            "estimated_latency_savings_ms",
            "estimated_cost_savings_usd",
        ]
        st.dataframe(queue_df[visible_cols], width="stretch", hide_index=True)

        selected_title = st.selectbox("Open queue item", queue_df["title"].tolist())
        item = next(row for row in queue if row["title"] == selected_title)
        st.markdown(f"### {item['title']}")
        c5, c6, c7, c8 = st.columns(4)
        c5.metric("Priority", item["priority_score"])
        c6.metric("Severity", item["severity"])
        c7.metric("Affected runs", item["run_count"])
        c8.metric("Patchable", item["patchable_count"])
        st.write(f"Type: `{item['type']}`")
        st.write(f"Status: `{item['status']}`")
        st.write(f"Estimated savings: {seconds(item['estimated_latency_savings_ms'])} / {money(item['estimated_cost_savings_usd'])}")
        st.write("Affected runs")
        st.code("\n".join(item["affected_runs"]), language="text")

        item_findings = [
            finding
            for finding in findings
            if finding["type"] == item["type"] and finding["title"] == item["title"]
        ]
        if item_findings:
            st.subheader("Finding instances")
            st.dataframe(pd.DataFrame(item_findings), width="stretch", hide_index=True)

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
            selected_issue = st.selectbox("Preview issue draft", [draft["title"] for draft in drafts])
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
    traces = store.list_traces(project_id=project_id)
    st.subheader("Optimization plans")
    if not traces:
        st.info("No traces stored for this project yet.")
    else:
        trace = select_trace(traces)
        if trace is not None:
            report = trace.report()
            plan = build_optimization_plan(trace, report)
            current = plan["current"]
            after = plan["estimated_after"]

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Current runtime", seconds(current["runtime_ms"]))
            c2.metric("Estimated runtime", seconds(after["runtime_ms"]))
            c3.metric("Latency reduction", f"{after['latency_reduction_pct']:.1f}%")
            c4.metric("Cost reduction", f"{after['cost_reduction_pct']:.1f}%")

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
            cards = plan["optimization_cards"]
            if not cards:
                st.info("No major optimization cards detected yet.")
            for card in cards:
                with st.expander(f"{card['title']} · confidence: {card['confidence']}", expanded=True):
                    st.write(card["why"])
                    st.code(card["rewrite_hint"])
                    st.write(f"Estimated latency savings: {seconds(card['estimated_latency_savings_ms'])}")
                    st.write(f"Estimated cost savings: {money(card['estimated_cost_savings_usd'])}")

            st.subheader("Execution graph")
            graph = plan["graph"]
            st.write("Bottlenecks")
            st.dataframe(pd.DataFrame(graph["bottlenecks"]), width="stretch")
            st.write("Parallelizable groups")
            if graph["parallelizable_groups"]:
                st.dataframe(pd.DataFrame(graph["parallelizable_groups"]), width="stretch")
            else:
                st.info("No obvious repeated independent tool-call groups found yet.")
            st.write("Edges")
            st.dataframe(pd.DataFrame(graph["edges"]), width="stretch")

            st.download_button(
                "Download optimization plan JSON",
                data=json.dumps(plan, indent=2),
                file_name="agentloop_optimization_plan.json",
                mime="application/json",
            )

elif page == "Diagnosis":
    traces = store.list_traces(project_id=project_id)
    st.subheader("Machine-actionable diagnosis")
    if not traces:
        st.info("No traces stored for this project yet.")
    else:
        trace = selected_trace_from_options(traces, "Choose trace for diagnosis")
        if trace is not None:
            diagnosis = build_diagnosis(trace)
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
                        st.dataframe(pd.DataFrame(finding["evidence"]), width="stretch", hide_index=True)

            st.download_button(
                "Download diagnosis JSON",
                data=json.dumps(diagnosis, indent=2),
                file_name="agentloop_diagnosis.json",
                mime="application/json",
            )

elif page == "Patch Plan":
    traces = store.list_traces(project_id=project_id)
    st.subheader("Trace-to-patch planning")
    if not traces:
        st.info("No traces stored for this project yet.")
    else:
        trace = selected_trace_from_options(traces, "Choose trace for patch planning")
        repo_path = st.text_input("Repository path", value=str(Path.cwd()))
        if trace is not None:
            plan = build_patch_plan(trace, repo_path=repo_path)
            summary = plan["summary"]
            c1, c2, c3 = st.columns(3)
            c1.metric("Patch plans", summary["patch_count"])
            c2.metric("Unsupported", summary["unsupported_finding_count"])
            c3.metric("Frameworks", ", ".join(summary["frameworks_detected"]) or "none")

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
                st.dataframe(pd.DataFrame(plan["unsupported_findings"]), width="stretch", hide_index=True)

            st.download_button(
                "Download patch plan JSON",
                data=json.dumps(plan, indent=2),
                file_name="agentloop_patch_plan.json",
                mime="application/json",
            )

elif page == "Replay Proof":
    traces = store.list_traces(project_id=project_id)
    st.subheader("Before/after replay proof")
    if len(traces) < 2:
        st.info("Store at least two traces to compare a baseline and candidate.")
    else:
        options = trace_options(traces)
        labels = list(options.keys())
        baseline_label = st.selectbox("Baseline trace", labels, index=0)
        candidate_label = st.selectbox("Candidate trace", labels, index=1 if len(labels) > 1 else 0)
        baseline = load_trace_for_project(options[baseline_label], project_id)
        candidate = load_trace_for_project(options[candidate_label], project_id)

        st.markdown("#### Gates")
        g1, g2, g3, g4 = st.columns(4)
        min_latency = g1.number_input("Min latency improvement %", min_value=0.0, value=20.0)
        min_cost = g2.number_input("Min cost improvement %", min_value=0.0, value=5.0)
        require_schema = g3.checkbox("Require schema valid", value=False)
        min_quality = g4.number_input("Min quality score", min_value=0.0, max_value=1.0, value=0.0)
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
            c3.metric("Cost improvement", f"{replay['deltas']['cost_improvement_pct']:.1f}%")
            c4.metric("Retry delta", replay["deltas"]["retry_count_delta"])

            st.subheader("Gate results")
            render_gate_table(replay["gates"]["results"])

            st.subheader("Metric deltas")
            metric_rows = [
                {"metric": key, "value": value}
                for key, value in replay["deltas"].items()
                if key.endswith("_delta") or key.endswith("_pct")
            ]
            st.dataframe(pd.DataFrame(metric_rows), width="stretch", hide_index=True)

            st.subheader("PR comment preview")
            st.code("Trace in. Rewrite plan out. Replay proof in the PR.\n\n" + ci_report_to_markdown(ci_report))

            col_json, col_md = st.columns(2)
            col_json.download_button(
                "Download replay JSON",
                data=json.dumps(replay, indent=2),
                file_name="agentloop_replay_report.json",
                mime="application/json",
            )
            col_md.download_button(
                "Download PR Markdown",
                data=ci_report_to_markdown(ci_report),
                file_name="agentloop_ci.md",
                mime="text/markdown",
            )

elif page == "Value & Pricing":
    traces = store.list_traces(project_id=project_id)
    st.subheader("Value report and pricing recommendation")
    st.caption("Turn an agent trace into buyer-facing ROI, reliability risk, and a conservative SaaS packaging recommendation.")
    if not traces:
        st.info("No traces stored for this project yet. Generate demo traces from the Ingest page first.")
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
            st.subheader("Pilot command")
            st.code(
                "agentloop value-report runs/research_agent_baseline.json "
                f"--runs-per-month {rpm} "
                f"--engineer-hourly-rate-usd {rate} "
                f"--incident-cost-usd {incident_cost} "
                "--out runs/value_report.json",
                language="bash",
            )

elif page == "API Keys":
    st.subheader("Create project API key")
    st.warning("API keys are only shown once. Copy the generated key before leaving this page.")
    key_name = st.text_input("Key name", value="local-dev")
    if st.button("Create API key"):
        created = store.create_api_key(project_id=project_id, name=key_name)
        st.success(f"Created key for project `{project_id}`")
        st.code(created["api_key"])

    st.subheader("Use this key")
    st.code(
        "agentloop upload runs/research_agent_baseline.json --api-url http://127.0.0.1:8000 --api-key YOUR_KEY",
        language="bash",
    )

elif page == "Ingest":
    st.subheader("Generate or upload traces")
    col_a, col_b, col_c, col_d = st.columns(4)
    generated_path: Path | None = None
    if col_a.button("Generate baseline"):
        generated_path = run_baseline(runs_dir)
    if col_b.button("Generate optimized"):
        generated_path = run_optimized(runs_dir)
    if col_c.button("Generate LangGraph-style"):
        generated_path = run_langgraph_style(runs_dir)
    if col_d.button("Generate all"):
        proof_baseline, proof_candidate = run_proof_pair(runs_dir)
        for path in [
            run_baseline(runs_dir),
            run_optimized(runs_dir),
            run_langgraph_style(runs_dir),
            proof_baseline,
            proof_candidate,
        ]:
            store.save_trace(AgentTrace.from_json(path), project_id=project_id)
        st.success(f"Generated and stored all demo traces under project `{project_id}`")

    if st.button("Generate proof demo pair"):
        proof_baseline, proof_candidate = run_proof_pair(runs_dir)
        for path in [proof_baseline, proof_candidate]:
            store.save_trace(AgentTrace.from_json(path), project_id=project_id)
        st.success(f"Generated and stored proof demo traces under project `{project_id}`")

    if generated_path is not None:
        trace = AgentTrace.from_json(generated_path)
        store.save_trace(trace, project_id=project_id)
        st.success(f"Generated and stored `{trace.name}` as `{trace.run_id}`")

    uploaded = st.file_uploader("Upload AgentLoop trace JSON", type=["json"])
    if uploaded is not None:
        trace = AgentTrace.from_dict(json.loads(uploaded.read().decode("utf-8")))
        if st.button("Store uploaded trace"):
            store.save_trace(trace, project_id=project_id)
            st.success(f"Stored uploaded trace `{trace.run_id}` under project `{project_id}`")

    st.subheader("CLI ingest")
    st.code(
        "agentloop demo-all\n"
        f"agentloop store-trace --path runs/research_agent_baseline.json --project-id {project_id}\n"
        f"agentloop list-stored-traces --project-id {project_id}",
        language="bash",
    )

elif page == "Setup":
    st.subheader("Local development")
    st.code(
        "pip install -e \".[all,dev]\"\n"
        "agentloop init-store\n"
        "agentloop demo-all\n"
        f"agentloop store-trace --path runs/research_agent_baseline.json --project-id {project_id}\n"
        "streamlit run dashboard/app.py",
        language="bash",
    )

    st.subheader("Hosted API")
    st.code(
        "agentloop create-api-key --project-id acme --name local-dev\n"
        "agentloop server --host 127.0.0.1 --port 8000\n"
        "agentloop upload runs/research_agent_baseline.json --api-url http://127.0.0.1:8000 --api-key al_xxx\n"
        "agentloop remote-usage --api-url http://127.0.0.1:8000 --api-key al_xxx",
        language="bash",
    )

    st.subheader("Postgres deployment")
    st.code(
        "export AGENTLOOP_STORE_BACKEND=postgres\n"
        "export AGENTLOOP_DATABASE_URL=postgresql://agentloop:agentloop@localhost:5432/agentloop\n"
        "agentloop init-store\n"
        "agentloop server --host 0.0.0.0 --port 8000",
        language="bash",
    )
