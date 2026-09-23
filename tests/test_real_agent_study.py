from __future__ import annotations

import io
import json
import sqlite3
import zipfile

import pytest

from agentloop import AgentTrace
from examples.real_agent_study.agent import ToolAgent
from examples.real_agent_study.api import LocalModel
from examples.real_agent_study.run import (
    freeze_pilot,
    implementation_hashes,
    load_protocol,
    run_case,
)
from examples.real_agent_study.scoring import SCORER_VERSION, grade, replay_scorer
from examples.real_agent_study.tools import RepositoryTools, WineDatabase, calculate

HEADER = '"fixed acidity";"volatile acidity";"citric acid";"residual sugar";"chlorides";"free sulfur dioxide";"total sulfur dioxide";"density";"pH";"sulphates";"alcohol";"quality"\n'
CSV = HEADER + "7;0.7;0;1.9;0.076;11;34;0.9978;3.51;0.56;9.4;5\n"


class FakeModel:
    def __init__(self, answers, trace=None):
        self.answers, self.trace = iter(answers), trace
        self.requests = []

    def complete(self, messages, **kwargs):
        self.requests.append(json.loads(json.dumps(messages)))
        return next(self.answers)


@pytest.mark.parametrize(
    "expression",
    ["__import__('os')", "open('x')", "[1]*99999", "2**99999", "True", "1e999", "9" * 500, "1/0"],
)
def test_calculator_rejects_code_and_unbounded_work(expression):
    with pytest.raises((ValueError, ZeroDivisionError)):
        calculate(expression)


def test_read_only_sql_and_bounded_results():
    database = WineDatabase(CSV, CSV)
    try:
        assert database.query("SELECT color,count(*) FROM wines GROUP BY color ORDER BY color")[
            "rows"
        ] == [["red", 1], ["white", 1]]
        for query in (
            "DELETE FROM wines",
            "ATTACH DATABASE 'x' AS x",
            "SELECT randomblob(1000000000)",
            "PRAGMA database_list",
            "SELECT load_extension('x')",
            "SELECT 1; SELECT 2",
        ):
            with pytest.raises((sqlite3.Error, sqlite3.Warning)):
                database.query(query)
        assert database.query("SELECT count(*) FROM wines")["rows"] == [[2]]
    finally:
        database.close()


def test_repository_tools_cannot_read_outside_snapshot():
    repository = RepositoryTools({"agentloop/a.py": "def target():\n    return 1\n"})
    assert repository.search("target")[0]["line"] == 1
    assert (
        repository.read({"path": "agentloop/a.py", "start": 2, "count": 1})[0]["text"]
        == "    return 1"
    )
    with pytest.raises(ValueError):
        repository.read({"path": "../../private.txt", "start": 1, "count": 1})


def test_custom_agent_uses_real_calculator_and_retains_call_limit():
    model = FakeModel(
        ['{"action":"calculate","argument":"6*7"}', '{"action":"final","answer":{"answer":"42"}}']
    )
    state = ToolAgent(
        "math", model, sources={}, red_csv="", white_csv="", max_calls=4, timeout_s=10
    ).run("Multiply six by seven")
    assert state["status"] == "completed" and state["history"][0]["result"] == 42
    assert "expected" not in json.dumps(model.requests)
    looping = FakeModel(['{"action":"calculate","argument":"6*7"}'] * 2)
    state = ToolAgent(
        "math", looping, sources={}, red_csv="", white_csv="", max_calls=2, timeout_s=10
    ).run("Multiply")
    assert state["status"] == "call_limit" and len(state["history"]) == 2


def test_real_langgraph_executes_model_and_tool_nodes():
    pytest.importorskip("langgraph.graph")
    model = FakeModel(
        [
            '{"action":"search","argument":"target"}',
            '{"action":"final","answer":{"path":"agentloop/a.py","symbol":"target","evidence_line":1,"evidence":"def target():"}}',
        ]
    )
    state = ToolAgent(
        "repository",
        model,
        sources={"agentloop/a.py": "def target():\n    return 1\n"},
        red_csv="",
        white_csv="",
        max_calls=4,
        timeout_s=10,
    ).run("Find the target")
    assert state["status"] == "completed" and state["history"][0]["result"][0]["line"] == 1


def test_quality_requires_independent_answer_and_real_tool_evidence():
    task = {"workload": "math", "expected": "42"}
    history = [{"tool": "calculate", "status": "ok", "result": 42}]
    assert grade({"answer": "42"}, task, sources={}, tool_history=history)["passed"]
    assert not grade({"answer": "42"}, task, sources={}, tool_history=[])["passed"]
    assert not grade({"answer": True}, task, sources={}, tool_history=history)["passed"]
    fixture = {"task": task}
    output = {
        "answer": {"answer": "41"},
        "tool_history": history,
        "status": "completed",
        "task_passed": True,
    }
    assert not replay_scorer(output, fixture, {"version": SCORER_VERSION})["passed"]
    output["answer"]["answer"] = "42"
    output["status"] = "timeout"
    assert not replay_scorer(output, fixture, {"version": SCORER_VERSION})["passed"]


def test_repository_quality_requires_quote_inside_correct_symbol():
    task = {
        "workload": "repository",
        "expected": {"path": "agentloop/a.py", "symbol": "target", "start": 1, "end": 2},
    }
    sources = {"agentloop/a.py": "def target():\n    return 1\n# unrelated evidence"}
    output = {
        "path": "agentloop/a.py",
        "symbol": "target",
        "evidence_line": 1,
        "evidence": "def target():",
    }
    history = [{"tool": "read", "status": "ok"}]
    assert grade(output, task, sources=sources, tool_history=history)["passed"]
    output.update(evidence_line=3, evidence="unrelated evidence")
    assert not grade(output, task, sources=sources, tool_history=history)["passed"]


def test_api_records_actual_usage_and_missing_usage(monkeypatch):
    import examples.real_agent_study.api as api

    trace = AgentTrace("test")
    model = LocalModel(
        "http://127.0.0.1:8766", {"id": "test"}, max_tokens=10, timeout_s=1, seed=1, trace=trace
    )
    result = {
        "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 2},
    }
    monkeypatch.setattr(
        api.urllib.request,
        "urlopen",
        lambda *args, **kwargs: io.BytesIO(json.dumps(result).encode()),
    )
    assert model.complete([], remaining_s=1) == "{}"
    assert trace.events[0].input_tokens == 12 and trace.events[0].token_provenance == "provider"
    result["usage"] = "invalid"
    model.complete([], remaining_s=1)
    assert trace.events[1].token_provenance == "unavailable"
    assert model.receipts[1]["usage"] is None


def test_api_failure_is_retained_and_remote_endpoints_rejected(monkeypatch):
    import examples.real_agent_study.api as api

    with pytest.raises(ValueError):
        LocalModel("https://example.com", {}, max_tokens=10, timeout_s=1, seed=1)
    trace = AgentTrace("test")
    model = LocalModel(
        "http://127.0.0.1:8766", {"id": "test"}, max_tokens=10, timeout_s=1, seed=1, trace=trace
    )

    def fail(*args, **kwargs):
        raise TimeoutError("private transport detail")

    monkeypatch.setattr(api.urllib.request, "urlopen", fail)
    with pytest.raises(TimeoutError):
        model.complete([], remaining_s=1)
    assert trace.events[0].status == "error" and model.receipts[0]["usage"] is None
    assert "private transport detail" not in json.dumps(model.receipts)


def test_frozen_protocol_rejects_code_drift_and_path_ids(tmp_path):
    source, destination = tmp_path / "draft.json", tmp_path / "protocol.json"
    source.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "tasks": [
                    {"id": "math-00", "workload": "math", "split": "pilot", "prompt": "Compute"}
                ],
                "source_manifest": {},
            }
        )
    )
    freeze_pilot(source, destination)
    assert load_protocol(destination, tmp_path)["implementation_hashes"] == implementation_hashes()
    value = json.loads(destination.read_text())
    value["tasks"][0]["id"] = "../../escape"
    destination.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        load_protocol(destination, tmp_path)


def test_failed_case_exports_original_trace_and_predictions(tmp_path, monkeypatch):
    import examples.real_agent_study.run as runner

    for name, value in (
        ("agentloop-v070-source.json", "{}"),
        ("winequality-red.csv", CSV),
        ("winequality-white.csv", CSV),
    ):
        (tmp_path / name).write_text(value)
    monkeypatch.setattr(
        runner.LocalModel,
        "complete",
        lambda *args, **kwargs: '{"action":"final","answer":{"answer":"wrong"}}',
    )
    protocol = {
        "models": {"baseline": {"id": "test"}},
        "scorer_version": SCORER_VERSION,
        "implementation_hashes": {},
        "max_output_tokens": 10,
        "request_timeout_s": 1,
        "max_model_calls": 2,
        "task_timeout_s": 10,
    }
    task = {"id": "math-00", "workload": "math", "prompt": "Compute 6*7", "expected": "42"}
    out = tmp_path / "case"
    result = run_case(protocol, task, "baseline", 0, tmp_path, out, "http://127.0.0.1:8766")
    assert not result["quality"]["passed"]
    assert (out / "trace.json").exists() and (out / "diagnosis.json").exists()
    assert result["trace_hash"] and result["diagnosis_hash"]
    with pytest.raises(ValueError):
        run_case(protocol, task, "baseline", 0, tmp_path, out, "http://127.0.0.1:8766")


def test_bundle_loader_validates_every_file_and_never_extracts(tmp_path):
    import hashlib

    from examples.real_agent_study.results import read_bundle

    contents = b'{"status":"failed"}'
    manifest = {
        "schema_version": "1.0",
        "files": {"receipt.json": hashlib.sha256(contents).hexdigest()},
    }
    path = tmp_path / "evidence.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("receipt.json", contents)
        archive.writestr("bundle-manifest.json", json.dumps(manifest))
    assert read_bundle(path) == manifest
    assert not (tmp_path / "receipt.json").exists()
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("receipt.json", b"changed")
        archive.writestr("bundle-manifest.json", json.dumps(manifest))
    with pytest.raises(ValueError, match="checksum"):
        read_bundle(path)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("../escape.json", contents)
        archive.writestr("bundle-manifest.json", json.dumps(manifest))
    with pytest.raises(ValueError, match="unsafe"):
        read_bundle(path)


def test_multipart_archive_roundtrip_and_tamper_guard(tmp_path):
    from examples.real_agent_study.archive import pack, unpack

    files = {"sources/data.json": b'{"a":1}', "runs/failed.json": b'{"status":"timeout"}'}
    parts = pack(files, tmp_path / "packed")
    assert len(parts) == 1
    assert unpack(tmp_path / "packed/index.json", tmp_path / "unpacked") == 2
    assert (tmp_path / "unpacked/runs/failed.json").read_bytes() == files["runs/failed.json"]
    with pytest.raises(ValueError):
        unpack(tmp_path / "packed/index.json", tmp_path / "unpacked")
    (tmp_path / "packed" / parts[0]["file"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        unpack(tmp_path / "packed/index.json", tmp_path / "bad")


def test_task_uncertainty_keeps_repetitions_within_tasks():
    from examples.real_agent_study.results import task_uncertainty

    result = task_uncertainty([("one", 10), ("one", 20), ("two", -5)])
    assert result["independent_tasks"] == 2 and result["pairs"] == 3
    assert result["task_mean_deltas_ms"] == {"one": 15, "two": -5}


def test_case_reader_keeps_missing_attempts_and_rejects_changed_predictions(tmp_path, monkeypatch):
    import examples.real_agent_study.run as runner
    from examples.real_agent_study.results import read_case
    from examples.real_agent_study.run import fingerprint

    task = {"id": "math-00", "workload": "math", "prompt": "Compute", "expected": "42"}
    assert read_case(tmp_path / "absent", "hash", task, "baseline", 0)["availability"] == "missing"
    for name, value in (
        ("agentloop-v070-source.json", "{}"),
        ("winequality-red.csv", CSV),
        ("winequality-white.csv", CSV),
    ):
        (tmp_path / name).write_text(value)
    protocol = {
        "models": {"baseline": {"id": "test"}},
        "scorer_version": SCORER_VERSION,
        "implementation_hashes": {},
        "max_output_tokens": 10,
        "request_timeout_s": 1,
        "max_model_calls": 2,
        "task_timeout_s": 10,
    }
    monkeypatch.setattr(
        runner.LocalModel,
        "complete",
        lambda *args, **kwargs: '{"action":"final","answer":{"answer":"42"}}',
    )
    run_case(protocol, task, "baseline", 0, tmp_path, tmp_path / "case", "http://127.0.0.1:8766")
    assert (
        read_case(tmp_path / "case", fingerprint(protocol), task, "baseline", 0)["availability"]
        == "recorded"
    )
    path = tmp_path / "case/diagnosis.json"
    changed = json.loads(path.read_text())
    changed["run_id"] = "different"
    path.write_text(json.dumps(changed))
    with pytest.raises(ValueError):
        read_case(tmp_path / "case", fingerprint(protocol), task, "baseline", 0)
