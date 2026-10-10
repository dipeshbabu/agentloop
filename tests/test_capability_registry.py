"""Declared vs current scoped observations without SDK or plugin imports."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace

import pytest
from typer.testing import CliRunner

from agentloop.entrypoint import app
from agentloop.harness import ALL_HOOKS, PYTHON_CAPABILITIES, Harness, HarnessConfig
from agentloop.interoperability.registry import (
    AdapterManifest,
    CapabilityDeclaration,
    CapabilityObservation,
    _probe_observation,
    builtin_adapters,
    capability_report,
    get_adapter,
    load_manifest,
    require_verified_boundary,
)
from agentloop.interoperability.validation import ImportValidationError


def observation(
    manifest, capability="enforcement.pre_tool", verdict="verified_supported", **changes
):
    declaration = next(item for item in manifest.capabilities if item.name == capability)
    return _probe_observation(
        **{
            "capability": capability,
            "verdict": verdict,
            "probe_id": "pretool-deny",
            "probe_version": "1.0",
            "tested_adapter_version": manifest.adapter_version,
            "tested_transport": manifest.transport,
            "manifest_sha256": manifest.fingerprint,
            "evidence_ref": "artifacts/pretool-deny.json",
            "evidence_sha256": "a" * 64,
            "environment_fingerprint": "b" * 64,
            "test_timestamp": "2026-01-01T00:00:00Z",
            "boundaries": declaration.boundaries,
            "source": "offline_behavioral",
            **changes,
        }
    )


def row(report, name):
    return next(item for item in report["capabilities"] if item["name"] == name)


def test_builtin_descriptors_derive_explicit_hooks_and_lifecycles():
    descriptors = {item.adapter_id: item for item in builtin_adapters()}
    assert set(descriptors) == {
        "python-wrapped",
        "langgraph-1.2.11",
        "harbor-atif",
        "omnigent-otel",
    }
    assert PYTHON_CAPABILITIES.hooks == ALL_HOOKS
    for adapter in ("python-wrapped", "langgraph-1.2.11"):
        manifest = descriptors[adapter]
        report = capability_report(manifest)
        assert row(report, "enforcement.pre_tool")["state"] == "declared_supported"
        assert row(report, "enforcement.pre_request")["state"] == "unknown"
        assert row(report, "lifecycle.async_stream")["state"] == "declared_supported"
        assert row(report, "compatibility.model_override")["state"] == "unknown"
        assert manifest.code_fingerprint
    assert descriptors["langgraph-1.2.11"].upstream_version == "1.2.11"


def test_enumeration_does_not_import_optional_sdks():
    code = "import sys; from agentloop.interoperability.registry import builtin_adapters; assert len(builtin_adapters())==4; assert not {'langgraph','omnigent','harbor','pydantic'} & set(sys.modules)"
    process = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert process.returncode == 0, process.stderr


def test_false_support_and_false_unsupported_declarations_report_drift():
    manifest = get_adapter("python-wrapped")
    failed = capability_report(manifest, [observation(manifest, verdict="verified_failed")])
    assert row(failed, "enforcement.pre_tool")["state"] == "verified_failed"
    assert failed["drift_status"] == "failed"
    declarations = tuple(
        replace(item, declared=False) if item.name == "enforcement.pre_tool" else item
        for item in manifest.capabilities
    )
    manifest = replace(manifest, capabilities=declarations)
    passed = capability_report(manifest, [observation(manifest)])
    assert row(passed, "enforcement.pre_tool")["declared"] is False
    assert row(passed, "enforcement.pre_tool")["state"] == "verified_supported"
    assert passed["drift"] == [
        {"capability": "enforcement.pre_tool", "declared": False, "observed": "verified_supported"}
    ]


@pytest.mark.parametrize(
    "missing",
    [
        "probe_id",
        "probe_version",
        "tested_adapter_version",
        "tested_transport",
        "manifest_sha256",
        "evidence_ref",
        "evidence_sha256",
        "environment_fingerprint",
        "test_timestamp",
    ],
)
def test_missing_probe_identity_cannot_be_verified(missing):
    manifest = get_adapter("python-wrapped")
    observed = observation(manifest, **{missing: None})
    report = capability_report(manifest, [observed])
    assert row(report, "enforcement.pre_tool")["state"] == "unknown"
    with pytest.raises(ImportValidationError, match="verified supported"):
        require_verified_boundary(manifest.adapter_id, "enforcement.pre_tool", [observed])


@pytest.mark.parametrize(
    "change",
    [
        {"tested_adapter_version": "future"},
        {"tested_transport": "native-tui"},
        {"manifest_sha256": "0" * 64},
        {"boundaries": ("model",)},
    ],
)
def test_version_transport_manifest_and_boundary_scope_do_not_transfer(change):
    manifest = get_adapter("python-wrapped")
    assert (
        row(capability_report(manifest, [observation(manifest, **change)]), "enforcement.pre_tool")[
            "state"
        ]
        == "unknown"
    )


def test_skipped_probe_is_not_a_failed_or_supported_capability():
    manifest = get_adapter("python-wrapped")
    observed = observation(
        manifest, verdict="skipped", reason="credentials_unavailable", evidence_ref=None
    )
    report = capability_report(manifest, [observed])
    assert row(report, "enforcement.pre_tool")["state"] == "skipped"
    assert not report["drift"]
    with pytest.raises(ImportValidationError, match="require a reason"):
        observation(manifest, verdict="skipped")


def test_offline_probe_cannot_verify_live_only_capability():
    manifest = get_adapter("python-wrapped")
    for capability in (
        "compatibility.model_override",
        "compatibility.approval",
        "compatibility.context_handling",
        "lifecycle.resume",
        "lifecycle.fork",
    ):
        result = capability_report(manifest, [observation(manifest, capability=capability)])
        assert row(result, capability)["state"] == "unknown"


def test_manifest_and_observation_serialization_remain_stable_but_external_claims_unverified():
    manifest = get_adapter("python-wrapped")
    assert AdapterManifest.from_dict(manifest.to_dict()).to_dict() == manifest.to_dict()
    assert AdapterManifest.from_dict(manifest.to_dict()).fingerprint == manifest.fingerprint
    observed = observation(manifest)
    loaded = CapabilityObservation.from_dict(observed.to_dict())
    assert loaded.to_dict() == observed.to_dict()
    assert row(capability_report(manifest, [loaded]), "enforcement.pre_tool")["state"] == "unknown"
    require_verified_boundary("python-wrapped", "enforcement.pre_tool", [observed])


@pytest.mark.parametrize("adapter", ["omnigent-otel", "harbor-atif"])
def test_observation_adapter_cannot_enable_runtime_enforcement(adapter):
    manifest = get_adapter(adapter)
    assert manifest.kind == "observation"
    result = capability_report(manifest)
    assert row(result, "enforcement.pre_tool")["state"] == "declared_unsupported"
    with pytest.raises(ImportValidationError, match="verified supported"):
        require_verified_boundary(adapter, "enforcement.pre_tool", [observation(manifest)])
    with pytest.raises(ImportValidationError, match="cannot declare"):
        replace(manifest, capabilities=(CapabilityDeclaration("enforcement.pre_tool", True),))


def test_catalog_loader_is_strict_data_only_and_runs_no_named_code(tmp_path):
    path = tmp_path / "catalog.json"
    manifest = get_adapter("python-wrapped").to_dict()
    manifest["adapter_id"] = "not.a.loaded.module"
    manifest["declaration_source"] = "never_execute.py"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    marker = tmp_path / "executed"
    (tmp_path / "never_execute.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8"
    )
    assert load_manifest(path).adapter_id == "not.a.loaded.module"
    assert not marker.exists()
    manifest["entry_point"] = "never_execute:run"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ImportValidationError, match="documented fields"):
        load_manifest(path)


def test_duplicate_declarations_and_observations_are_not_silently_overwritten():
    manifest = get_adapter("python-wrapped")
    with pytest.raises(ImportValidationError, match="unique"):
        replace(manifest, capabilities=(manifest.capabilities[0], manifest.capabilities[0]))
    observed = observation(manifest)
    with pytest.raises(ImportValidationError, match="unique"):
        capability_report(manifest, [observed, observed])


def test_cli_lists_and_inspects_capabilities_with_explicit_unknowns(tmp_path):
    runner = CliRunner()
    listing = runner.invoke(app, ["harness", "list-adapters"])
    assert listing.exit_code == 0, listing.output
    assert len(json.loads(listing.output)) == 4
    out = tmp_path / "capabilities.json"
    result = runner.invoke(
        app, ["harness", "capabilities", "--adapter", "omnigent-otel", "--json-out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert (
        row(json.loads(out.read_text()), "enforcement.pre_tool")["state"] == "declared_unsupported"
    )
    invalid = runner.invoke(app, ["harness", "capabilities", "--adapter", "unregistered"])
    assert invalid.exit_code != 0


def test_registry_does_not_change_existing_wrapper_defaults():
    config = HarnessConfig()
    run = Harness(config).start_run()

    def original():
        return 7

    assert run.wrap(original, boundary="tool") is original
    builtin_adapters()
    assert config.mode == "disabled" and run.wrap(original, boundary="tool")() == 7


@pytest.mark.parametrize(
    "field,value", [("kind", {}), ("capabilities", None), ("schema_version", "future")]
)
def test_malformed_manifest_is_a_structured_error(field, value):
    value_dict = get_adapter("python-wrapped").to_dict()
    value_dict[field] = value
    with pytest.raises(ImportValidationError):
        AdapterManifest.from_dict(value_dict)


def test_external_source_never_becomes_current_behavioral_proof():
    manifest = get_adapter("python-wrapped")
    result = capability_report(manifest, [observation(manifest, source="external_reported")])
    assert row(result, "enforcement.pre_tool")["state"] == "unknown"


def test_probe_timestamp_requires_valid_timezone_qualified_iso():
    manifest = get_adapter("python-wrapped")
    for stamp in ("not-a-time", "2026-01-01T00:00:00"):
        with pytest.raises(ImportValidationError):
            observation(manifest, test_timestamp=stamp)
