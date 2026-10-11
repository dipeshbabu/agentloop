from __future__ import annotations

import importlib.util
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from typer.testing import CliRunner

import agentloop
from agentloop.entrypoint import _analysis_payload, app
from agentloop.integrations.hermes import export_hermes_plugin, register_hermes
from agentloop.integrations.hermes_recorder import HOOKS, HermesRecorder
from agentloop.integrations.sdk import instrument as instrument_sdk
from agentloop.quality import build_quality_report
from agentloop.schema import SCHEMA_VERSION
from agentloop.tracer import AgentTrace


class Context:
    def __init__(self, **settings):
        self.settings = settings
        self.hooks = {}
        self.unload = []

    def register_hook(self, name, callback):
        assert name not in self.hooks
        self.hooks[name] = callback

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def on_unload(self, callback):
        self.unload.append(callback)

    def emit(self, name, **payload):
        assert self.hooks[name](**payload) is None


def scope(turn="turn-1", session="session-1", task="task-1"):
    return {"session_id": session, "task_id": task, "turn_id": turn}


def model(recorder, identity, *, request="request-1", retry=0, failed=False, usage=None):
    start = time.time()
    recorder.pre_api_request(
        **identity, api_request_id=request, retry_count=retry, started_at=start
    )
    end = time.time()
    fields = {
        **identity,
        "api_request_id": request,
        "model": "gpt-4.1-mini",
        "provider": "openai",
        "started_at": start,
        "ended_at": end,
        "api_duration": end - start,
    }
    if failed:
        recorder.api_request_error(
            **fields,
            retry_count=retry,
            retryable=True,
            error={"message": "PRIVATE_ERROR"},
            status_code=429,
        )
    else:
        recorder.post_api_request(**fields, usage=usage)


def read(recorder):
    return [AgentTrace.from_json(path) for path in recorder.exported_paths]


def test_records_accounting_tools_retry_and_one_turn_without_raw_content(tmp_path):
    recorder = HermesRecorder(tmp_path)
    identity = scope()
    recorder.pre_llm_call(
        **identity, user_message="PRIVATE_PROMPT", conversation_history=["PRIVATE_HISTORY"]
    )
    model(recorder, identity, failed=True)
    model(
        recorder,
        identity,
        request="request-2",
        retry=1,
        usage={
            "input_tokens": 80,
            "cache_read_tokens": 15,
            "cache_write_tokens": 5,
            "output_tokens": 10,
            "reasoning_tokens": 4,
            "prompt_tokens": 100,
        },
    )
    recorder.post_tool_call(
        **identity,
        tool_name="search",
        tool_call_id="tool-1",
        duration_ms=0,
        status="success",
        args={"secret": "PRIVATE_ARGS"},
        result="PRIVATE_RESULT",
    )
    recorder.post_llm_call(**identity, assistant_response="PRIVATE_FINAL")
    recorder.on_session_end(**identity, completed=True)
    recorder.on_session_end(**identity, completed=True)
    recorder.on_session_finalize(session_id=identity["session_id"])
    [trace] = read(recorder)
    assert trace.to_dict()["schema_version"] == SCHEMA_VERSION
    report = trace.report()
    assert report["model_call_count"] == 2
    assert report["retry_count"] == 1
    assert report["input_tokens"] == 100
    assert report["output_tokens"] == 10  # reasoning is a subset, not added twice
    requests = [event for event in trace.events if event.event_type == "model_call"]
    assert [event.status for event in requests] == ["error", "ok"]
    assert [event.token_provenance for event in requests] == ["unavailable", "provider"]
    assert all(event.parent_id == trace.events[0].event_id for event in trace.events[1:])
    assert trace.metadata["hermes"]["turn_outcome"] == "completed"
    assert trace.metadata["hermes"]["capture_complete"] is True
    assert "PRIVATE_" not in recorder.exported_paths[0].read_text(encoding="utf-8")
    assert _analysis_payload(trace)["report"]["analysis_complete"] is True


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"input_tokens": True, "output_tokens": 2},
        {"prompt_tokens": -1, "output_tokens": 2},
    ],
)
def test_missing_or_invalid_usage_stays_unavailable(tmp_path, usage):
    recorder = HermesRecorder(tmp_path)
    recorder.pre_llm_call(**scope())
    model(recorder, scope(), usage=usage)
    recorder.on_session_end(**scope(), completed=True)
    [trace] = read(recorder)
    event = trace.events[1]
    assert event.token_provenance == "unavailable"
    assert event.input_tokens == event.output_tokens == 0


def test_capture_is_explicit_and_oversized_output_cannot_be_quality_evidence(tmp_path):
    recorder = HermesRecorder(tmp_path, capture_content=True)
    recorder.pre_llm_call(**scope(), user_message="PRIVATE_PROMPT")
    recorder.post_tool_call(
        **scope(),
        tool_call_id="tool-1",
        tool_name="local",
        status="success",
        duration_ms=0,
        result="tool output",
        args={"secret": "PRIVATE_ARGS"},
    )
    recorder.post_llm_call(**scope(), assistant_response="correct answer")
    recorder.on_session_end(**scope(), completed=True)
    trace = read(recorder)[0]
    assert trace.metadata["output"] == "correct answer"
    assert trace.events[1].output_text == "tool output"
    assert "PRIVATE_" not in json.dumps(trace.to_dict())
    recorder.pre_llm_call(**scope("turn-2"))
    recorder.post_llm_call(**scope("turn-2"), assistant_response="x" * 10001)
    recorder.on_session_end(**scope("turn-2"), completed=True)
    trace = read(recorder)[1]
    assert "output" not in trace.metadata
    assert trace.metadata["hermes"]["output_omitted_too_large"] is True


@pytest.mark.parametrize("outcome", ["failed", "interrupted"])
def test_failed_and_interrupted_turns_are_retained(tmp_path, outcome):
    recorder = HermesRecorder(tmp_path)
    recorder.pre_llm_call(**scope())
    recorder.on_session_end(**scope(), **{outcome: True})
    [trace] = read(recorder)
    assert trace.events[0].status == "error"
    assert trace.metadata["hermes"]["turn_outcome"] == outcome


def test_teardown_closes_only_its_own_session_and_preserves_incomplete_attempts(tmp_path):
    recorder = HermesRecorder(tmp_path)
    recorder.pre_llm_call(**scope(session="a"))
    recorder.pre_llm_call(**scope(session="b"))
    recorder.pre_api_request(
        **scope(session="a"), api_request_id="inflight", started_at=time.time()
    )
    recorder.on_session_finalize(session_id="a")
    assert recorder.summary()["active_turns"] == 1
    [trace] = read(recorder)
    assert trace.metadata["hermes"]["unfinished_requests"] == 1
    assert trace.metadata["hermes"]["capture_complete"] is False
    recorder.flush()
    assert len(read(recorder)) == 2


def test_concurrent_sessions_stay_separate(tmp_path):
    recorder = HermesRecorder(tmp_path)

    def capture(index):
        identity = scope(turn="shared-turn", task="shared-task", session=f"session-{index}")
        recorder.pre_llm_call(**identity)
        model(recorder, identity, usage={"prompt_tokens": index, "output_tokens": 1})
        recorder.on_session_end(**identity, completed=True)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(capture, range(12)))
    traces = read(recorder)
    assert len(traces) == len({trace.run_id for trace in traces}) == 12
    assert sorted(trace.events[1].input_tokens for trace in traces) == list(range(12))
    assert len({trace.metadata["hermes"]["session_id"] for trace in traces}) == 12


def test_limits_and_missing_correlation_remain_explicit(tmp_path):
    recorder = HermesRecorder(tmp_path, max_active_turns=1, max_events=1)
    recorder.pre_llm_call(**scope())
    recorder.pre_llm_call(**scope("other"))
    recorder.post_tool_call(tool_name="orphan", duration_ms=0, status="success")
    for index in range(3):
        recorder.post_tool_call(
            **scope(), tool_name="tool", tool_call_id=str(index), duration_ms=0, status="success"
        )
    recorder.on_session_end(**scope(), completed=True)
    [trace] = read(recorder)
    assert len(trace.events) == 2
    assert trace.metadata["hermes"]["events_dropped"] == 2
    assert trace.metadata["hermes"]["capture_complete"] is False
    assert recorder.summary()["unscoped_events"] == 2


def test_export_failure_is_fail_open_and_flush_can_retry(tmp_path, monkeypatch):
    recorder = HermesRecorder(tmp_path)
    recorder.pre_llm_call(**scope())
    original = Path.open

    def fail_open(path, *args, **kwargs):
        if path.suffix == ".tmp":
            raise PermissionError("PRIVATE_CREDENTIAL_IN_ERROR")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_open)
    recorder.on_session_end(**scope(), completed=True)
    assert recorder.summary()["export_errors"] == recorder.summary()["pending_exports"] == 1
    assert not list(tmp_path.glob("*.json"))
    monkeypatch.setattr(Path, "open", original)
    recorder.flush()
    assert len(read(recorder)) == 1
    assert recorder.summary()["pending_exports"] == 0
    assert not list(tmp_path.glob("*.tmp"))


def test_child_relationships_do_not_copy_goals_or_double_count_usage(tmp_path):
    recorder = HermesRecorder(tmp_path)
    recorder.pre_llm_call(**scope())
    fields = {
        "parent_session_id": "session-1",
        "parent_turn_id": "turn-1",
        "child_session_id": "child-session",
        "child_subagent_id": "child-id",
    }
    recorder.subagent_start(**fields, child_goal="PRIVATE_GOAL")
    recorder.subagent_stop(**fields, child_status="completed", usage={"input_tokens": 999})
    recorder.on_session_end(**scope(), completed=True)
    [trace] = read(recorder)
    assert trace.metadata["hermes"]["children"]["child-id"]["status"] == "completed"
    assert trace.report()["input_tokens"] == 0
    assert "PRIVATE_GOAL" not in json.dumps(trace.to_dict())


def test_generated_plugin_runs_without_importing_agentloop_and_profiles_are_isolated(
    tmp_path, monkeypatch
):
    output = export_hermes_plugin(tmp_path / "plugin")
    manifest = json.loads((output / "plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["provides_hooks"] == list(HOOKS)
    assert "python_dependencies" not in manifest
    assert (
        (output / "LICENSE")
        .read_text(encoding="utf-8")
        .startswith("                                 Apache License")
    )
    module_name = "standalone_hermes_agentloop"
    spec = importlib.util.spec_from_file_location(
        module_name, output / "__init__.py", submodule_search_locations=[str(output)]
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    hermes = ModuleType("hermes_constants")
    home = tmp_path / "profile-a"
    hermes.get_hermes_home = lambda: home
    monkeypatch.setitem(sys.modules, "hermes_constants", hermes)
    spec.loader.exec_module(module)
    a, b = Context(), Context()
    module.register(a)
    module.register(a)  # same-context registration is idempotent
    home = tmp_path / "profile-b"
    module.register(b)
    assert set(a.hooks) == set(b.hooks) == set(HOOKS)
    for ctx in (a, b, a):
        identity = scope(turn=str(time.time_ns()))
        ctx.emit("pre_llm_call", **identity)
        ctx.emit("on_session_end", **identity, completed=True)
    assert len(list((tmp_path / "profile-a/agentloop/traces").glob("*.json"))) == 2
    assert len(list((tmp_path / "profile-b/agentloop/traces").glob("*.json"))) == 1
    source = (output / "recorder.py").read_text(encoding="utf-8")
    assert "from agentloop" not in source and "import agentloop" not in source
    assert len(a.unload) == 1
    a.unload[0]()


def test_plugin_export_refuses_existing_directory(tmp_path):
    existing = tmp_path / "keep"
    existing.mkdir()
    sentinel = existing / "mine"
    sentinel.write_text("preserve", encoding="utf-8")
    with pytest.raises(ValueError, match="already exists"):
        export_hermes_plugin(existing)
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_duplicate_request_callbacks_do_not_add_usage_or_unfinished_requests(tmp_path):
    recorder = HermesRecorder(tmp_path)
    recorder.pre_llm_call(**scope())
    start = time.time()
    fields = {
        **scope(),
        "api_request_id": "req",
        "started_at": start,
        "ended_at": start,
        "api_duration": 0,
        "model": "model",
        "usage": {"prompt_tokens": 10, "output_tokens": 2},
    }
    for _ in range(2):
        recorder.pre_api_request(**scope(), api_request_id="req", started_at=start)
        recorder.post_api_request(**fields)
    recorder.on_session_end(**scope(), completed=True)
    [trace] = read(recorder)
    assert trace.report()["model_call_count"] == 1
    assert trace.report()["input_tokens"] == 10
    assert trace.metadata["hermes"]["unfinished_requests"] == 0


def test_sdk_message_objects_support_explicit_output_capture(tmp_path):
    recorder = HermesRecorder(tmp_path, capture_content=True)
    recorder.pre_llm_call(**scope())
    now = time.time()
    recorder.pre_api_request(**scope(), api_request_id="req", started_at=now)
    recorder.post_api_request(
        **scope(),
        api_request_id="req",
        started_at=now,
        ended_at=now,
        api_duration=0,
        assistant_message=SimpleNamespace(content="answer"),
    )
    recorder.on_session_end(**scope(), completed=True)
    [trace] = read(recorder)
    assert trace.events[1].output_text == "answer"


def test_missing_final_output_cannot_fall_back_to_an_intermediate_model_response(tmp_path):
    recorder = HermesRecorder(tmp_path, capture_content=True)
    recorder.pre_llm_call(**scope())
    now = time.time()
    recorder.pre_api_request(**scope(), api_request_id="req", started_at=now)
    recorder.post_api_request(
        **scope(),
        api_request_id="req",
        started_at=now,
        ended_at=now,
        api_duration=0,
        assistant_message={"content": "intermediate"},
    )
    recorder.post_llm_call(**scope(), assistant_response="x" * 10001)
    recorder.on_session_end(**scope(), completed=True)
    [trace] = read(recorder)
    report = build_quality_report([{"expected": "intermediate"}], candidate_trace=trace)
    assert report["passed"] is False


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True, "invalid", 1e308])
def test_malformed_timing_and_status_cannot_break_capture(tmp_path, value):
    recorder = HermesRecorder(tmp_path)
    recorder.pre_llm_call(**scope())
    recorder.post_api_request(
        **scope(),
        api_request_id="req",
        started_at=value,
        ended_at=value,
        api_duration=value,
        usage={"prompt_tokens": 1, "output_tokens": 1},
    )
    recorder.post_tool_call(
        **scope(),
        tool_call_id="tool",
        tool_name="local",
        duration_ms=value,
        status={"invalid": "PRIVATE_STATUS"},
    )
    recorder.on_session_end(**scope(), completed=True)
    [trace] = read(recorder)
    assert trace.metadata["hermes"]["capture_complete"] is False
    assert "PRIVATE_STATUS" not in json.dumps(trace.to_dict())


def test_existing_or_competing_export_is_never_replaced(tmp_path):
    recorder = HermesRecorder(tmp_path)
    recorder.pre_llm_call(**scope())
    state = next(iter(recorder._turns.values()))
    target = tmp_path / (state["trace"]["run_id"] + ".json")
    target.write_text("existing artifact", encoding="utf-8")
    recorder.on_session_end(**scope(), completed=True)
    assert target.read_text(encoding="utf-8") == "existing artifact"
    assert recorder.summary()["export_errors"] == 1
    assert not list(tmp_path.glob("*.tmp"))


def test_partial_serialization_is_cleaned_up_and_retry_succeeds(tmp_path, monkeypatch):
    recorder = HermesRecorder(tmp_path)
    recorder.pre_llm_call(**scope())
    original = json.dump

    def fail_dump(payload, handle, *args, **kwargs):
        handle.write("partial")
        raise OSError("write interrupted")

    monkeypatch.setattr(json, "dump", fail_dump)
    recorder.on_session_end(**scope(), completed=True)
    assert not list(tmp_path.iterdir())
    monkeypatch.setattr(json, "dump", original)
    recorder.flush()
    assert len(read(recorder)) == 1


def test_failed_export_buffer_is_bounded(tmp_path, monkeypatch):
    recorder = HermesRecorder(tmp_path)

    def deny_publish(*args, **kwargs):
        raise PermissionError("not writable")

    import os

    monkeypatch.setattr(os, "link", deny_publish)
    for index in range(20):
        identity = scope(turn=f"turn-{index}")
        recorder.pre_llm_call(**identity)
        recorder.on_session_end(**identity, completed=True)
    assert recorder.summary()["pending_exports"] == 16
    assert recorder.summary()["dropped_exports"] == 4


@pytest.mark.parametrize(
    "options",
    [{"capture_content": 1}, {"max_events": True}, {"max_events": 0}, {"max_active_turns": 0}],
)
def test_invalid_limits_are_rejected_before_registration(tmp_path, options):
    with pytest.raises(ValueError):
        HermesRecorder(tmp_path, **options)


def test_cli_exports_plugin_package(tmp_path):
    output = tmp_path / "plugin"
    result = CliRunner().invoke(app, ["hermes", "export-plugin", str(output)])
    assert result.exit_code == 0, result.output
    assert (output / "recorder.py").exists()
    assert "hermes plugins enable agentloop" in result.output


def test_public_startup_helper_is_explicit_and_idempotent(tmp_path, monkeypatch):
    constants = ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: tmp_path
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    ctx = Context()
    assert agentloop.instrument(ctx, integration="hermes", enabled=False) is ctx
    assert not ctx.hooks
    recorder = agentloop.instrument(ctx, integration="hermes", enabled=True)
    assert isinstance(recorder, HermesRecorder)
    assert instrument_sdk(ctx, kind="hermes") is register_hermes(ctx) is recorder
    assert set(ctx.hooks) == set(HOOKS)
    with pytest.raises(TypeError, match="PluginContext"):
        agentloop.instrument(object(), integration="hermes", enabled=True)
