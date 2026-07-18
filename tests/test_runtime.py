from __future__ import annotations

from pathlib import Path

import pytest

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


def test_finalize_runs_later_destination_after_earlier_failure(tmp_path: Path, monkeypatch) -> None:
    agentloop.reset_runtime()
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(tmp_path / "store.db"))
    # Export target is unwritable (its parent is a file), but store should still run.
    agentloop.init(export_dir=blocker / "sub", auto_store=True, fail_silently=True)

    with trace_agent("independence") as trace:
        with trace_tool_call("tool"):
            pass

    result = trace.finalize_result
    assert result is not None
    assert result["exported_path"] is None
    assert result["stored"] is True
    assert [error["destination"] for error in result["errors"]] == ["export"]

    agentloop.reset_runtime()


def test_finalize_collects_multiple_destination_errors(tmp_path: Path) -> None:
    agentloop.reset_runtime()
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    agentloop.init(
        export_dir=blocker / "sub",
        api_url="http://127.0.0.1:9",
        auto_upload=True,
        fail_silently=True,
    )

    with trace_agent("multi") as trace:
        with trace_tool_call("tool"):
            pass

    result = trace.finalize_result
    assert result is not None
    assert {error["destination"] for error in result["errors"]} == {"export", "upload"}
    assert result["uploaded"] is False

    agentloop.reset_runtime()


def test_successful_finalization_clears_stale_error(tmp_path: Path) -> None:
    agentloop.reset_runtime()
    agentloop.init(api_url="http://127.0.0.1:9", auto_upload=True, fail_silently=True)

    with trace_agent("failing") as trace:
        with trace_tool_call("tool"):
            pass
    assert trace.finalize_result["errors"]
    assert agentloop.get_last_error() is not None

    # Reconfigure to a working export-only destination; a clean run clears the error.
    agentloop.init(export_dir=tmp_path, auto_upload=False)
    with trace_agent("recovered") as recovered:
        with trace_tool_call("tool"):
            pass
    assert recovered.finalize_result["exported_path"] is not None
    assert recovered.finalize_result["errors"] == []
    assert agentloop.get_last_error() is None

    agentloop.reset_runtime()


def test_non_silent_mode_raises_finalization_error_with_partial_result(tmp_path: Path) -> None:
    agentloop.reset_runtime()
    # Export (first) succeeds, upload (second) fails; fail-fast raises but keeps the export.
    agentloop.init(
        export_dir=tmp_path,
        api_url="http://127.0.0.1:9",
        auto_upload=True,
        fail_silently=False,
    )

    with pytest.raises(agentloop.FinalizationError) as excinfo:
        with trace_agent("non_silent"):
            with trace_tool_call("tool"):
                pass

    error = excinfo.value
    assert error.result["exported_path"] is not None
    assert error.errors[-1]["destination"] == "upload"
    assert agentloop.get_last_error() is not None

    agentloop.reset_runtime()


def test_clear_sentinel_resets_optional_values(monkeypatch) -> None:
    monkeypatch.delenv("AGENTLOOP_API_KEY", raising=False)
    monkeypatch.delenv("AGENTLOOP_EXPORT_DIR", raising=False)
    agentloop.reset_runtime()

    agentloop.init(api_key="al_secret", export_dir="runs/exports")
    assert agentloop.get_runtime_config().api_key == "al_secret"
    assert agentloop.get_runtime_config().export_dir is not None

    # None keeps the current values.
    agentloop.init(project_id="demo")
    assert agentloop.get_runtime_config().api_key == "al_secret"
    assert agentloop.get_runtime_config().export_dir is not None

    # CLEAR resets them explicitly.
    agentloop.init(api_key=agentloop.CLEAR, export_dir=agentloop.CLEAR)
    assert agentloop.get_runtime_config().api_key is None
    assert agentloop.get_runtime_config().export_dir is None

    agentloop.reset_runtime()
