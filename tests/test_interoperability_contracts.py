"""Offline negative contracts for the additive external evidence boundary."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentloop.interoperability.contracts import (
    ImportReceipt,
    external_id,
    receipt_id,
    summarize_receipts,
)
from agentloop.interoperability.validation import (
    ImportBudget,
    ImportLimits,
    ImportValidationError,
    load_json_artifact,
    parse_json_bytes,
    relative_reference,
    safe_artifact_path,
    validate_json_tree,
)
from agentloop.otel import traces_from_otel
from agentloop.tracer import AgentTrace

FIXTURES = Path(__file__).parent / "fixtures" / "external"


def read(path):
    return load_json_artifact(FIXTURES, path).payload


def receipt_payload():
    return read("expected/imported_receipts.json")[0]


def scoring_contract(thresholds):
    contract = {
        "schema_version": "1.0",
        "scorer_id": "blue-cards-v1",
        "rule": "all_gte",
        "thresholds": thresholds,
    }
    encoded = json.dumps(contract, sort_keys=True, separators=(",", ":"))
    return {**contract, "config_sha256": sha256(encoded.encode()).hexdigest()}


def test_missing_receipt_is_immutable_and_does_not_invent_measurements():
    payload = receipt_payload()
    receipt = ImportReceipt.from_dict(payload)
    payload["source_metadata"]["exception_type"] = "changed"
    copy = receipt.to_dict()
    copy["outcome"]["quality_pass"] = True
    retained = receipt.to_dict()
    assert retained["source_metadata"]["exception_type"] == "AgentError"
    assert retained["traces"] == []
    assert retained["outcome"]["quality_pass"] is None
    assert retained["outcome"]["verifier_dimensions"] == {}
    assert retained["completeness"]["cost"] == "unknown"
    assert not any(
        key in retained for key in ("duration_ms", "input_tokens", "cost_usd", "success")
    )


def test_inventory_reconciles_missing_and_uninterpreted_receipts():
    receipts = [ImportReceipt.from_dict(value) for value in read("expected/imported_receipts.json")]
    report = summarize_receipts(receipts)
    assert report == read("expected/counters_and_completeness.json")
    assert (
        report["receipts"]
        == report["quality_pass"] + report["quality_fail"] + report["quality_indeterminate"]
    )
    assert report["without_native_traces"] == 2
    assert report["external_quality_uninterpreted"] == 1
    with pytest.raises(ImportValidationError, match="duplicate receipt"):
        summarize_receipts([receipts[0], receipts[0]])


def test_external_identity_does_not_alias_tuples_types_jobs_or_shared_sessions():
    pairs = [
        ({"job_id": "ab", "trial_id": "c"}, {"job_id": "a", "trial_id": "bc"}),
        ({"step_id": 1}, {"step_id": "1"}),
        ({"job_id": "a", "trial_id": "same"}, {"job_id": "b", "trial_id": "same"}),
        (
            {"session_id": "same", "trajectory_id": "a"},
            {"session_id": "same", "trajectory_id": "b"},
        ),
        ({"artifact_sha256": "a" * 64}, {"artifact_sha256": "b" * 64}),
    ]
    for left, right in pairs:
        assert external_id("run", "harbor", left) != external_id("run", "harbor", right)
    identity = {"job_id": "a", "trial_id": "same"}
    assert external_id("run", "harbor", identity) == external_id(
        "run", "harbor", dict(reversed(list(identity.items())))
    )
    assert external_id("run", "harbor", identity) != external_id("run", "omnigent", identity)
    for invalid in ({}, {"trial_id": None}, {"step_id": True}, {"trial_id": []}):
        with pytest.raises(ImportValidationError):
            external_id("run", "harbor", invalid)


@pytest.mark.parametrize(
    "verifier_status", ["available", "missing", "failed", "invalid", "unknown"]
)
def test_positive_reward_never_becomes_a_pass_without_scoring(verifier_status):
    payload = receipt_payload()
    payload["outcome"].update(
        verifier_status=verifier_status, verifier_dimensions={"correctness": 0.5}, quality_pass=True
    )
    with pytest.raises(ImportValidationError, match="explicit scoring contract"):
        ImportReceipt.from_dict(payload)
    payload["outcome"]["quality_pass"] = None
    assert (
        ImportReceipt.from_dict(payload).to_dict()["outcome"]["verifier_dimensions"]["correctness"]
        == 0.5
    )


@pytest.mark.parametrize(
    "dimensions,expected",
    [
        ({"correctness": 1.0, "coverage": 1.0}, True),
        ({"correctness": 1.0, "coverage": 0.5}, False),
        ({"correctness": 1.0}, None),
        ({"correctness": 1.0, "coverage": None}, None),
    ],
)
def test_required_dimensions_and_exact_thresholds_control_quality(dimensions, expected):
    payload = receipt_payload()
    payload["outcome"].update(
        verifier_status="available",
        verifier_dimensions=dimensions,
        quality_basis="configured_thresholds",
        quality_pass=expected,
        scoring_contract=scoring_contract({"correctness": 1.0, "coverage": 1.0}),
    )
    assert ImportReceipt.from_dict(payload).to_dict()["outcome"]["quality_pass"] is expected
    payload["outcome"]["quality_pass"] = not expected
    with pytest.raises(ImportValidationError, match="configured thresholds"):
        ImportReceipt.from_dict(payload)


def test_failed_verifier_and_changed_scoring_hash_fail_closed():
    payload = receipt_payload()
    payload["outcome"].update(
        verifier_status="failed",
        verifier_dimensions={"correctness": 1.0},
        quality_pass=None,
        quality_basis="configured_thresholds",
        scoring_contract=scoring_contract({"correctness": 1.0}),
    )
    ImportReceipt.from_dict(payload)
    payload["outcome"]["quality_pass"] = True
    with pytest.raises(ImportValidationError):
        ImportReceipt.from_dict(payload)
    payload["outcome"]["quality_pass"] = None
    payload["outcome"]["scoring_contract"]["thresholds"]["correctness"] = 0.5
    with pytest.raises(ImportValidationError, match="scoring definition"):
        ImportReceipt.from_dict(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("thresholds", {}),
        ("thresholds", {"correctness": True}),
        ("thresholds", {"correctness": float("nan")}),
        ("thresholds", {"correctness": "1.0"}),
        ("rule", "reward_positive"),
    ],
)
def test_ambiguous_scoring_contracts_are_rejected(field, value):
    payload = receipt_payload()
    contract = scoring_contract({"correctness": 1.0})
    contract[field] = value
    payload["outcome"].update(quality_basis="configured_thresholds", scoring_contract=contract)
    with pytest.raises(ImportValidationError):
        ImportReceipt.from_dict(payload)


def test_multiple_native_trace_references_are_validated_without_fabricated_tree():
    payload = receipt_payload()
    payload["traces"] = [
        {"run_id": "run_a", "trace_file": "traces/a.json", "trace_sha256": "a" * 64},
        {"run_id": "run_b", "trace_file": "traces/b.json", "trace_sha256": "b" * 64},
    ]
    payload["missing_trace_reason"] = None
    payload["relationships"] = [
        {
            "kind": "span_link",
            "target_id": "external-child",
            "basis": "external_reported",
            "resolved": False,
        }
    ]
    receipt = ImportReceipt.from_dict(payload)
    assert summarize_receipts([receipt])["native_traces"] == 2
    assert "parent_id" not in receipt.to_dict()["relationships"][0]
    payload["traces"][1]["run_id"] = "run_a"
    with pytest.raises(ImportValidationError, match="duplicate native"):
        ImportReceipt.from_dict(payload)


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_field",
        "future_version",
        "missing_reason",
        "unknown_absent_identity_basis",
        "bad_hash",
        "changed_identity",
        "invalid_task_digest",
        "boolean_reward",
        "trace_path_escape",
        "large_metadata",
        "raw_notice_message",
    ],
)
def test_invalid_or_ambiguous_receipts_are_rejected(mutation):
    payload = receipt_payload()
    if mutation == "unknown_field":
        payload["extra"] = {}
    elif mutation == "future_version":
        payload["schema_version"] = "2.0"
    elif mutation == "missing_reason":
        payload["missing_trace_reason"] = None
    elif mutation == "unknown_absent_identity_basis":
        payload["identity_provenance"]["job_id"] = "external_reported"
    elif mutation == "bad_hash":
        payload["source"]["artifact_sha256"] = "not-a-hash"
    elif mutation == "changed_identity":
        payload["external_identity"]["trial_id"] = "another-trial"
    elif mutation == "invalid_task_digest":
        payload["external_identity"]["task_digest"] = "sha256:wrong"
    elif mutation == "boolean_reward":
        payload["outcome"]["verifier_dimensions"] = {"correctness": True}
    elif mutation == "trace_path_escape":
        payload["traces"] = [
            {"run_id": "run_a", "trace_file": "../outside.json", "trace_sha256": "a" * 64}
        ]
        payload["missing_trace_reason"] = None
    elif mutation == "large_metadata":
        payload["source_metadata"] = {"blob": "x" * (64 * 1024)}
    else:
        payload["notices"][0]["message"] = "raw payload must never appear in diagnostics"
    with pytest.raises(ImportValidationError):
        ImportReceipt.from_dict(payload)


def test_source_reference_or_hash_changes_receipt_identity():
    payload = receipt_payload()
    old = receipt_id(payload)
    for field, value in (
        ("artifact_reference", "other/result.json"),
        ("artifact_sha256", "b" * 64),
    ):
        changed = deepcopy(payload)
        changed["source"][field] = value
        changed["receipt_id"] = receipt_id(changed)
        assert ImportReceipt.from_dict(changed).receipt_id != old


@pytest.mark.parametrize(
    "data,code",
    [
        (b"\xff", "invalid_json"),
        (b'{"x":1,"x":2}', "duplicate_key"),
        (b'{"x":NaN}', "invalid_number"),
        (b'{"x":Infinity}', "invalid_number"),
        (b'{"x":1e999}', "invalid_number"),
        (b'"\\ud800"', "invalid_json"),
        (b"[{]", "invalid_json"),
    ],
)
def test_hostile_json_is_rejected_without_payload_echo(data, code):
    with pytest.raises(ImportValidationError) as exc:
        parse_json_bytes(data)
    assert exc.value.code == code
    assert "NaN" not in str(exc.value)
    assert "Infinity" not in str(exc.value)
    assert "x" not in exc.value.reason


def test_bounds_apply_before_recursive_decode_and_on_python_inputs():
    limits = ImportLimits(max_json_bytes=64, max_depth=3, max_nodes=5, max_number_chars=10)
    literal = {"literal": '[[[[[{"}'}
    assert parse_json_bytes(json.dumps(literal).encode(), limits) == literal
    for value in (b" " * 65, b"[[[[]]]]", b"[1,2,3,4,5]", b"12345678901"):
        with pytest.raises(ImportValidationError) as exc:
            parse_json_bytes(value, limits)
        assert exc.value.code == "limit_exceeded"
    cycle = []
    cycle.append(cycle)
    for value in (cycle, {1: "nonstring key"}, (1, 2), float("inf"), 10**200, "\ud800"):
        with pytest.raises(ImportValidationError):
            validate_json_tree(value, limits)
    for field, value in (
        ("max_json_bytes", 0),
        ("max_depth", 129),
        ("max_records", True),
        ("max_records", 1.5),
    ):
        with pytest.raises(ImportValidationError):
            replace(ImportLimits(), **{field: value})


def test_artifact_loader_hashes_exact_bytes_and_budgets_invalid_records(tmp_path):
    data = b'{ "source" : true }\n'
    (tmp_path / "valid.json").write_bytes(data)
    (tmp_path / "invalid.json").write_bytes(b"{INVALID")
    limits = ImportLimits(max_records=2)
    budget = ImportBudget(limits)
    artifact = load_json_artifact(tmp_path, "valid.json", limits=limits, budget=budget)
    assert artifact.artifact_sha256 == sha256(data).hexdigest()
    assert artifact.payload == {"source": True}
    assert artifact.byte_count == len(data)
    with pytest.raises(ImportValidationError):
        load_json_artifact(tmp_path, "invalid.json", limits=limits, budget=budget)
    assert budget.total_bytes == len(data) + len(b"{INVALID")
    assert budget.records == 2
    with pytest.raises(ImportValidationError, match="budget exceeded"):
        load_json_artifact(tmp_path, "valid.json", limits=limits, budget=budget)
    with pytest.raises(ImportValidationError, match="limits must match"):
        load_json_artifact(tmp_path, "valid.json", budget=budget)
    with pytest.raises(ImportValidationError):
        budget.consume(trials=limits.max_trials + 1, references=1)
    assert budget.references == budget.trials == 0


def test_byte_budget_and_artifact_size_are_enforced(tmp_path):
    (tmp_path / "large.json").write_bytes(b" " * 100 + b"{}")
    for limits in (ImportLimits(max_json_bytes=16), ImportLimits(max_total_bytes=16)):
        with pytest.raises(ImportValidationError) as exc:
            load_json_artifact(tmp_path, "large.json", limits=limits, budget=ImportBudget(limits))
        assert exc.value.code == "limit_exceeded"


def test_frozen_hostile_references_never_read_external_paths(tmp_path):
    for reference in read("hostile_cases.json")["unsafe_references"]:
        with pytest.raises(ImportValidationError) as exc:
            safe_artifact_path(tmp_path, reference)
        assert exc.value.code == "unsafe_path"
    with pytest.raises(ImportValidationError):
        relative_reference("x/" * 33 + "a.json")
    with pytest.raises(ImportValidationError):
        relative_reference("bad\ud800.json")
    (tmp_path / "directory").mkdir()
    with pytest.raises(ImportValidationError, match="regular file"):
        safe_artifact_path(tmp_path, "directory")
    with pytest.raises(ImportValidationError) as exc:
        safe_artifact_path(tmp_path, "absent.json")
    assert exc.value.code == "missing_artifact"


def test_symlink_sources_and_symlink_directories_are_rejected(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    (tmp_path / "external.json").write_text('{"secret":"OUTSIDE"}', encoding="utf-8")
    try:
        (root / "escape.json").symlink_to(tmp_path / "external.json")
        (root / "nested").symlink_to(tmp_path, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"OS does not permit symlink creation: {type(exc).__name__}")
    for reference in ("escape.json", "nested/external.json"):
        with pytest.raises(ImportValidationError) as exc:
            load_json_artifact(root, reference)
        assert exc.value.code == "unsafe_path"
        assert "OUTSIDE" not in str(exc.value)


def test_windows_reparse_attributes_are_rejected_without_new_path_apis(tmp_path, monkeypatch):
    file = tmp_path / "reparse.json"
    file.write_text('{"source":"MUST_NOT_BE_READ"}', encoding="utf-8")
    original_lstat = Path.lstat

    def lstat(path):
        result = original_lstat(path)
        if path == file:
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
        return result

    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(Path, "is_junction", lambda path: False, raising=False)
    with pytest.raises(ImportValidationError) as exc:
        load_json_artifact(tmp_path, "reparse.json")
    assert exc.value.code == "unsafe_path"
    assert "MUST_NOT_BE_READ" not in str(exc.value)


def test_frozen_fixture_inventory_is_complete_and_pinned():
    matrix = read("source_matrix.json")
    assert len(matrix["families"]) == 15
    assert len({family["family"] for family in matrix["families"]}) == 15
    assert matrix["generated"] == "owned_synthetic"
    for path, expected_hash in matrix["files_sha256"].items():
        assert sha256((FIXTURES / path).read_bytes()).hexdigest() == expected_hash
    for source in matrix["sources"]:
        assert len(source["revision"]) == 40
        assert source["license"] == "Apache-2.0"
    for family in matrix["families"]:
        assert (FIXTURES / family["path"]).exists()
        assert family["expected"]


def test_frozen_hostile_json_cases_have_payload_free_diagnostics():
    for case in read("hostile_cases.json")["json_cases"]:
        with pytest.raises(ImportValidationError) as exc:
            parse_json_bytes(case["data"].encode("utf-8"))
        assert exc.value.code == case["code"]
        assert "FIRST" not in str(exc.value)
        assert "SECOND" not in str(exc.value)


def test_mixed_job_freezes_complete_source_inventory_without_executing_it():
    root = FIXTURES / "harbor/mixed_job"
    results = {
        path.parent.name: read(path.relative_to(FIXTURES).as_posix())
        for path in root.glob("*/result.json")
    }
    assert set(results) == {
        "pass",
        "quality_fail",
        "exception",
        "timeout",
        "cancelled",
        "missing_trajectory",
        "uninterpreted",
        "multistep",
    }
    assert results["quality_fail"]["verifier_result"]["rewards"] == {"correctness": 0}
    assert results["uninterpreted"]["verifier_result"]["rewards"] == {"correctness": 0.5}
    assert results["uninterpreted"]["verifier_environment_mode"] == "shared"
    assert results["pass"]["verifier_environment_mode"] == "separate"
    for name in ("exception", "timeout", "cancelled"):
        assert results[name]["exception_info"]["exception_type"]
        assert results[name]["verifier_result"] is None
    assert not (root / "missing_trajectory/agent/trajectory.json").exists()
    assert [
        step["verifier_result"]["rewards"]["correctness"]
        for step in results["multistep"]["step_results"]
    ] == [1.0, 0.0]


def test_atif_fixtures_freeze_noninterchangeable_source_semantics():
    embedded = read("harbor/atif_embedded_subagents.json")
    docs = [embedded, *embedded["subagent_trajectories"]]
    assert len({doc["trajectory_id"] for doc in docs}) == 3
    assert len({doc["session_id"] for doc in docs}) == 1
    assert embedded["final_metrics"]["total_prompt_tokens"] == 36
    assert sum(doc["steps"][1]["metrics"]["prompt_tokens"] for doc in docs) == 36
    deterministic = read("harbor/atif_deterministic_dispatch.json")
    assert deterministic["steps"][0]["llm_call_count"] == 0
    assert "metrics" not in deterministic["steps"][0]
    aggregated = read("harbor/atif_aggregated_llm_calls.json")
    assert aggregated["steps"][1]["llm_call_count"] == 3
    assert "ended_at" not in aggregated["steps"][1]
    media = read("harbor/atif_v18_multimodal.json")
    assert media["steps"][0]["message"][2]["type"] == "audio"
    assert "reasoning_content" in media["steps"][1]
    assert not (FIXTURES / "harbor/media/card.png").exists()


def test_otlp_fixture_native_parity_and_links_are_not_parent_edges():
    traces = traces_from_otel(read("omnigent/agent_tool_policy.otlp.json"))
    assert len(traces) == 1
    assert {event.operation_kind for event in traces[0].events} == {"agent", "tool", "guardrail"}
    policy = next(event for event in traces[0].events if event.operation_kind == "guardrail")
    assert policy.metadata["policy.action"] == "DENY"
    assert "agentloop.harness" not in traces[0].metadata
    multi = traces_from_otel(read("omnigent/parent_child_multi_trace.otlp.json"))
    assert len(multi) == 2
    assert all(event.parent_id is None for trace in multi for event in trace.events)
    assert (
        sum(len(event.metadata.get("otel_links", [])) for trace in multi for event in trace.events)
        == 1
    )
    for trace in [*traces, *multi]:
        assert AgentTrace.from_dict(trace.to_dict()).to_dict() == trace.to_dict()


def test_core_and_contract_imports_do_not_load_external_runtimes():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import agentloop; import agentloop.interoperability.contracts; assert not any(n == 'harbor' or n.startswith('harbor.') or n == 'omnigent' or n.startswith('omnigent.') for n in sys.modules)",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert not result.stdout
    for file in (Path(__file__).parents[1] / "agentloop/interoperability").glob("*.py"):
        ast.parse(file.read_text(encoding="utf-8"), feature_version=(3, 10))
