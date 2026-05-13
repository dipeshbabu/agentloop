from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from agentloop.demo import run_baseline, run_langgraph_style, run_optimized
from agentloop.optimizer import build_optimization_plan
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


st.sidebar.title("AgentLoop Cloud")
st.sidebar.caption("Hosted control panel for agent-loop performance")

project_id = st.sidebar.text_input("Project", value=st.session_state.get("project_id", "default"))
st.session_state["project_id"] = project_id

page = st.sidebar.radio(
    "Navigate",
    ["Overview", "Traces", "Optimization", "Value & Pricing", "API Keys", "Ingest", "Setup"],
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
        st.dataframe(df, use_container_width=True)

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
        st.dataframe(df, use_container_width=True)
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
                st.dataframe(events[cols], use_container_width=True)

            st.download_button(
                "Download trace JSON",
                data=json.dumps(trace.to_dict(), indent=2),
                file_name=f"{trace.run_id}.json",
                mime="application/json",
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
            st.dataframe(pd.DataFrame(graph["bottlenecks"]), use_container_width=True)
            st.write("Parallelizable groups")
            if graph["parallelizable_groups"]:
                st.dataframe(pd.DataFrame(graph["parallelizable_groups"]), use_container_width=True)
            else:
                st.info("No obvious repeated independent tool-call groups found yet.")
            st.write("Edges")
            st.dataframe(pd.DataFrame(graph["edges"]), use_container_width=True)

            st.download_button(
                "Download optimization plan JSON",
                data=json.dumps(plan, indent=2),
                file_name="agentloop_optimization_plan.json",
                mime="application/json",
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
        for path in [run_baseline(runs_dir), run_optimized(runs_dir), run_langgraph_style(runs_dir)]:
            store.save_trace(AgentTrace.from_json(path), project_id=project_id)
        st.success(f"Generated and stored all demo traces under project `{project_id}`")

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
        f"agentloop store-trace runs/research_agent_baseline.json --project-id {project_id}\n"
        f"agentloop list-stored-traces --project-id {project_id}",
        language="bash",
    )

elif page == "Setup":
    st.subheader("Local development")
    st.code(
        "pip install -e \".[all,dev]\"\n"
        "agentloop init-store\n"
        "agentloop demo-all\n"
        f"agentloop store-trace runs/research_agent_baseline.json --project-id {project_id}\n"
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
