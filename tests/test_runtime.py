from __future__ import annotations

from pathlib import Path

import agentloop
from agentloop import trace_agent, trace_tool_call


def test_runtime_exports_trace_on_context_exit(tmp_path: Path) -> None:
    agentloop.reset_runtime()
    agentloop.init(export_dir=tmp_path, auto_store=False, auto_upload=False)

    with trace_agent("runtime_export_test") as trace:
        with trace_tool_call("tool"):
            pass

    assert trace.finalize_result is not None
    assert trace.finalize_result["exported_path"] is not None
    exported = Path(trace.finalize_result["exported_path"])
    assert exported.exists()
    assert exported.parent == tmp_path

    agentloop.reset_runtime()


def test_runtime_records_errors_when_fail_silently() -> None:
    agentloop.reset_runtime()
    agentloop.init(api_url="http://127.0.0.1:9", auto_upload=True, fail_silently=True)

    with trace_agent("runtime_upload_error_test") as trace:
        with trace_tool_call("tool"):
            pass

    assert trace.finalize_result is not None
    assert trace.finalize_result["uploaded"] is False
    assert trace.finalize_result["errors"]
    assert agentloop.get_last_error() is not None

    agentloop.reset_runtime()
