"""Incremental inventory export retains every row and refuses conflicting bytes."""

import json
from pathlib import Path

import pytest

from agentloop.integrations.harbor.trials import import_harbor
from agentloop.interoperability.artifacts import write_artifact_chunks
from agentloop.interoperability.validation import ImportValidationError


def test_harbor_jsonl_inventory_preserves_summary_and_all_negative_rows(tmp_path):
    result = import_harbor(Path(__file__).parent / "fixtures/external/harbor/mixed_job")
    result.write(tmp_path)
    inventory = result.inventory()
    path = tmp_path / "harbor-trials.jsonl"
    before = path.read_bytes()
    with path.open(encoding="utf-8") as stream:
        summary = json.loads(next(stream))
        rows = [json.loads(line)["trial"] for line in stream]
    assert summary["record_type"] == "summary"
    assert summary["summary"] == {
        key: value for key, value in inventory.items() if key != "trial_rows"
    }
    assert rows == inventory["trial_rows"]
    assert any(row["import_status"] != "imported" for row in rows)
    result.write(tmp_path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("existing", [b"one\ntwo\n", b"one\nchanged\n", b"one\ntwo\nextra"])
def test_chunk_comparison_checks_late_records_and_trailing_content(tmp_path, existing):
    path = tmp_path / "inventory.jsonl"
    path.write_bytes(existing)
    chunks = (chunk for chunk in (b"one\n", b"two\n"))
    if existing == b"one\ntwo\n":
        write_artifact_chunks(tmp_path, path.name, chunks)
    else:
        with pytest.raises(ImportValidationError):
            write_artifact_chunks(tmp_path, path.name, chunks)
    assert path.read_bytes() == existing


@pytest.mark.parametrize("error", [RuntimeError, OSError, KeyboardInterrupt])
def test_interrupted_new_export_removes_partial_file_and_allows_complete_retry(tmp_path, error):
    def chunks():
        yield b"header\n"
        raise error("export interrupted")

    with pytest.raises(error, match="export interrupted"):
        write_artifact_chunks(tmp_path, "inventory.jsonl", chunks())
    assert not (tmp_path / "inventory.jsonl").exists()
    write_artifact_chunks(tmp_path, "inventory.jsonl", (b"header\n", b"row\n"))
    assert (tmp_path / "inventory.jsonl").read_bytes() == b"header\nrow\n"


def test_failed_comparison_does_not_remove_existing_complete_artifact(tmp_path):
    path = tmp_path / "inventory.jsonl"
    path.write_bytes(b"header\nrow\n")

    def chunks():
        yield b"header\n"
        raise RuntimeError("producer failed")

    with pytest.raises(RuntimeError, match="producer failed"):
        write_artifact_chunks(tmp_path, path.name, chunks())
    assert path.read_bytes() == b"header\nrow\n"


def test_disk_write_error_removes_partial_file_and_allows_retry(tmp_path, monkeypatch):
    target = tmp_path / "inventory.jsonl"
    original_open = Path.open

    class FailedWrite:
        def __init__(self):
            self.stream = original_open(target, "xb")

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.stream.close()

        def write(self, chunk):
            self.stream.write(chunk[:2])
            raise OSError("disk write failed")

    def open_with_failure(path, *args, **kwargs):
        return (
            FailedWrite()
            if path == target and args == ("xb",)
            else original_open(path, *args, **kwargs)
        )

    with monkeypatch.context() as context:
        context.setattr(Path, "open", open_with_failure)
        with pytest.raises(OSError, match="disk write failed"):
            write_artifact_chunks(tmp_path, target.name, (b"header\n",))
    assert not target.exists()
    write_artifact_chunks(tmp_path, target.name, (b"complete\n",))
    assert target.read_bytes() == b"complete\n"


def test_exclusive_open_failure_preserves_competing_writer_artifact(tmp_path, monkeypatch):
    target = tmp_path / "inventory.jsonl"
    original_open = Path.open

    def competing_open(path, *args, **kwargs):
        if path == target and args == ("xb",):
            with original_open(target, "xb") as stream:
                stream.write(b"other writer\n")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", competing_open)
    with pytest.raises(FileExistsError):
        write_artifact_chunks(tmp_path, target.name, (b"our writer\n",))
    assert target.read_bytes() == b"other writer\n"
