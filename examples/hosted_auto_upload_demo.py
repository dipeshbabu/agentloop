from __future__ import annotations

import os
import time

import agentloop
from agentloop import trace_agent, trace_model_call, trace_tool_call

agentloop.init(
    api_url=os.getenv("AGENTLOOP_API_URL", "http://127.0.0.1:8000"),
    api_key=os.getenv("AGENTLOOP_API_KEY"),
    project_id=os.getenv("AGENTLOOP_PROJECT_ID", "demo"),
    auto_upload=os.getenv("AGENTLOOP_AUTO_UPLOAD", "false").lower() in {"1", "true", "yes"},
    auto_store=os.getenv("AGENTLOOP_AUTO_STORE", "true").lower() in {"1", "true", "yes"},
    export_dir=os.getenv("AGENTLOOP_EXPORT_DIR", "runs/auto"),
)


with trace_agent("hosted_auto_upload_demo", metadata={"demo": True}) as trace:
    with trace_model_call("plan", model="gpt-demo", input_tokens=1200, output_tokens=180):
        time.sleep(0.02)

    with trace_tool_call("search_docs", metadata={"tool": "search"}):
        time.sleep(0.01)

    with trace_model_call("synthesize", model="gpt-demo", input_tokens=900, output_tokens=260):
        time.sleep(0.02)

print(f"run_id={trace.run_id}")
print(f"finalize_result={trace.finalize_result}")
