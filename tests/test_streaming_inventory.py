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
