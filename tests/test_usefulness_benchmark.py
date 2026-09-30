from __future__ import annotations

import json

import pytest

from examples.usefulness_benchmark.historical import historical_fingerprint
from examples.usefulness_benchmark.protocol import fingerprint, freeze, load_protocol
from examples.usefulness_benchmark.reference import execute_references
from examples.usefulness_benchmark.report import summarize, validate_reference_row


@pytest.fixture(scope="module")
def reference_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("usefulness-benchmark")
    path = root / "protocol.json"
    protocol = freeze(path, source_revision="0" * 40, repetitions=1)
    result = execute_references(protocol, root)
    return root, protocol, result


def test_protocol_binds_sources_tasks_and_independent_labels(tmp_path):
    path = tmp_path / "protocol.json"
    protocol = freeze(path, source_revision="0" * 40, repetitions=1)
    assert load_protocol(path) == protocol
    assert len(protocol["workloads"]) == 5
    assert "examples/real_agent_study/archive.py" in protocol["analyzer"]["sources"]
    assert "examples/real_agent_study/results.py" in protocol["analyzer"]["sources"]
    assert {item["split"] for item in protocol["workloads"]} == {"development", "evaluation"}
    email = protocol["workloads"][0]
    for case in email["cases"]:
        label = next(
            item for item in case["finding_labels"] if item["rule_id"] == "semantic_redundancy"
        )
        assert label["label"] == (
            "no_opportunity" if case["input"]["verification"] else "opportunity"
        )
    protocol["workloads"][0]["cases"].clear()
    path.write_text(json.dumps(protocol))
    with pytest.raises(ValueError, match="hash"):
        load_protocol(path)


def test_all_variants_failures_and_predictions_are_retained(reference_run):
    root, protocol, result = reference_run
    expected = sum(len(item["cases"]) * len(item["variants"]) for item in protocol["workloads"])
    assert len(result["rows"]) == expected == 111
    assert any(row["candidate_error_spans"] for row in result["rows"])
    assert any(
        row["quality"]["candidate_score"] is not None and row["quality"]["candidate_score"] < 1
        for row in result["rows"]
    )
    assert all(
        row["prediction_receipt"]["frozen_at"] <= row["candidate_started_at"]
        for row in result["rows"]
    )
    assert all(
        row["attribution"] == "combined_configuration_unattributed" for row in result["rows"]
    )
    assert (
        result["finding_evaluation"]["results"]["evaluation"]["summary"]["overall"]["planned_cases"]
        > 0
    )


def test_offline_report_does_not_execute_workloads_and_is_stable(reference_run, monkeypatch):
    import examples.usefulness_benchmark.reference as references

    def forbidden(*args, **kwargs):
        pytest.fail("offline reporting must not execute a workload")

    monkeypatch.setattr(references, "_run", forbidden)
    root, _, _ = reference_run
    first = summarize(root)
    assert summarize(root) == first
    assert first["reference_complete"]
    assert first["historical_available"] is False
    assert len(first["workload_results"]) == 15
    assert all(
        row["runtime_change_ms"]["independent_task_count"] > 0 for row in first["workload_results"]
    )
    assert first["manual_effort"]["operator_minutes"] is None


def test_tampered_prediction_fails_validation(reference_run, tmp_path):
    import shutil

    root, protocol, result = reference_run
    workload = protocol["workloads"][0]
    case = workload["cases"][0]
    slot = fingerprint([workload["id"], case["id"], 0])[:20]
    destination = tmp_path / "reference" / workload["id"] / slot
    shutil.copytree(root / "reference" / workload["id"] / slot, destination)
    prediction = destination / "prediction.json"
    value = json.loads(prediction.read_text())
    value["name"] = "changed"
    prediction.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="prediction"):
        validate_reference_row(tmp_path, workload, case, 0, workload["variants"][0])


def test_partial_run_keeps_missing_slots_in_planned_denominator(tmp_path):
    protocol = freeze(tmp_path / "protocol.json", source_revision="0" * 40, repetitions=1)
    result = summarize(tmp_path)
    assert not result["reference_complete"]
    assert sum(row["planned_pairs"] for row in result["workload_results"]) == 111
    assert sum(row["recorded_pairs"] for row in result["workload_results"]) == 0
    assert all(
        row["runtime_change_ms"]["summary"]["mean"] is None for row in result["workload_results"]
    )
    assert result["finding_labels"]["synthetic_reference"]["status"] == "unavailable"
    assert protocol["paid_provider_budget_usd"] == 0


def test_historical_unicode_hash_is_not_rewritten_to_native_hash():
    value = {"text": "source — café"}
    assert historical_fingerprint(value) != fingerprint(value)
    assert historical_fingerprint(value) == historical_fingerprint(json.loads(json.dumps(value)))


def test_source_change_rejected_before_execution(tmp_path):
    path = tmp_path / "protocol.json"
    protocol = freeze(path, source_revision="0" * 40, repetitions=1)
    first = next(iter(protocol["analyzer"]["sources"]))
    protocol["analyzer"]["sources"][first] = "0" * 64
    protocol.pop("sha256")
    protocol["sha256"] = fingerprint(protocol)
    path.write_text(json.dumps(protocol))
    with pytest.raises(ValueError, match="source differs"):
        load_protocol(path)
