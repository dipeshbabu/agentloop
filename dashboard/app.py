from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from agentloop.demo import run_baseline, run_langgraph_style, run_optimized
from agentloop.tracer import AgentTrace

st.set_page_config(page_title="AgentLoop", layout="wide")
st.title("AgentLoop Profiler")
st.caption("Performance engineering for AI agent loops")

runs_dir = Path("runs")
runs_dir.mkdir(parents=True, exist_ok=True)

st.subheader("Demo traces")
col_a, col_b, col_c, col_d = st.columns(4)
if col_a.button("Generate baseline"):
    run_baseline(runs_dir)
if col_b.button("Generate optimized"):
    run_optimized(runs_dir)
if col_c.button("Generate LangGraph-style"):
    run_langgraph_style(runs_dir)
if col_d.button("Generate all"):
    run_baseline(runs_dir)
    run_optimized(runs_dir)
    run_langgraph_style(runs_dir)

run_files = sorted(runs_dir.glob("*.json"))
uploaded = st.file_uploader("Upload AgentLoop trace JSON", type=["json"])
selected = None
if run_files:
    selected = st.selectbox("Or select a local run", run_files, format_func=lambda p: p.name)

if uploaded is not None:
    trace = AgentTrace.from_dict(json.loads(uploaded.read().decode("utf-8")))
elif selected is not None:
    trace = AgentTrace.from_json(selected)
else:
    st.info("Generate a demo trace above, run `agentloop demo-all`, or upload a trace JSON.")
    st.stop()

report = trace.report()

st.subheader("Run summary")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Runtime", f"{report['total_runtime_ms'] / 1000:.2f}s")
c2.metric("Estimated cost", f"${report['estimated_cost_usd']:.4f}")
c3.metric("Model calls", report["model_call_count"])
c4.metric("Tool calls", report["tool_call_count"])

c5, c6, c7, c8 = st.columns(4)
c5.metric("Input tokens", report["input_tokens"])
c6.metric("Output tokens", report["output_tokens"])
c7.metric("Retries", report["retry_count"])
c8.metric("Repeated context", f"{report['repeated_context_ratio']:.1%}")

st.subheader("Latency breakdown")
breakdown = pd.DataFrame([
    {"category": "model", "ms": report["model_time_ms"]},
    {"category": "tool", "ms": report["tool_time_ms"]},
    {"category": "retry", "ms": report["retry_time_ms"]},
])
st.bar_chart(breakdown, x="category", y="ms")

st.subheader("Timeline")
events = pd.DataFrame(report["events"])
cols = ["event_type", "name", "duration_ms", "model", "input_tokens", "output_tokens", "status"]
for col in cols:
    if col not in events.columns:
        events[col] = None
if not events.empty:
    st.dataframe(events[cols], use_container_width=True)

st.subheader("Recommendations")
for rec in report["recommendations"]:
    st.markdown(f"**{rec['title']}**")
    st.write(rec["description"])

if report["parallelism_opportunities"]:
    st.subheader("Parallelism opportunities")
    st.dataframe(pd.DataFrame(report["parallelism_opportunities"]), use_container_width=True)
