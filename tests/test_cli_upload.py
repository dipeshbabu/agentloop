from __future__ import annotations

import pytest
from typer.testing import CliRunner

import agentloop.cli as cli_module
from agentloop.cli import app
from agentloop.tracer import AgentTrace

runner = CliRunner()


@pytest.fixture
def uploads(monkeypatch):
    sent: list[AgentTrace] = []

    class CapturingClient:
        def __init__(self, base_url, api_key=None, admin_api_key=None):  # type: ignore[no-untyped-def]
            self.base_url = base_url

        def upload_trace(self, trace):  # type: ignore[no-untyped-def]
            sent.append(trace)
            return {"run_id": trace.run_id, "project_id": "team"}

    monkeypatch.setattr(cli_module, "AgentLoopClient", CapturingClient)
    return sent


def _trace(path, name, metadata=None):
    trace = AgentTrace(name=name, metadata=metadata or {})
    trace.finish()
    trace.export_json(path)
    return trace


def test_upload_sends_files_and_directories_with_labels(tmp_path, uploads):
    single = _trace(tmp_path / "single.json", "single", {"labels": {"owner": "kept"}})
    batch = tmp_path / "batch"
    batch.mkdir()
    _trace(batch / "b.json", "b")
    _trace(batch / "a.json", "a")
    (batch / "notes.txt").write_text("ignored", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "upload",
            "--path",
            str(tmp_path / "single.json"),
            "--path",
            str(batch),
            "--label",
            "commit=abc123",
            "--label",
            "branch=feature/x=y",
            "--api-url",
            "https://agentloop.example",
        ],
    )
    assert result.exit_code == 0, result.output
    assert [trace.name for trace in uploads] == ["single", "a", "b"]
    assert uploads[0].run_id == single.run_id
    assert uploads[0].metadata["labels"] == {
        "owner": "kept",
        "commit": "abc123",
        "branch": "feature/x=y",
    }
    assert uploads[1].metadata["labels"] == {"commit": "abc123", "branch": "feature/x=y"}
    assert "Uploaded 3 traces." in result.output
    assert "Project: team" in result.output


@pytest.mark.parametrize("bad", ["no-equals", "=value", "bad key=1", "k=" + "v" * 501])
def test_upload_rejects_malformed_labels_before_sending(tmp_path, uploads, bad):
    _trace(tmp_path / "t.json", "t")
    result = runner.invoke(app, ["upload", "--path", str(tmp_path / "t.json"), "--label", bad])
    assert result.exit_code == 2
    assert uploads == []


def test_upload_validates_every_file_before_sending_any(tmp_path, uploads):
    _trace(tmp_path / "good.json", "good")
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    result = runner.invoke(
        app,
        ["upload", "--path", str(tmp_path / "good.json"), "--path", str(tmp_path / "broken.json")],
    )
    assert result.exit_code == 2
    assert uploads == []


def test_upload_rejects_an_empty_directory(tmp_path, uploads):
    (tmp_path / "empty").mkdir()
    result = runner.invoke(app, ["upload", "--path", str(tmp_path / "empty")])
    assert result.exit_code == 2
    assert "No .json trace files" in result.output
