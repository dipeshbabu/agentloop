"""Actual offline behavior, dishonest drivers, skips and deterministic reports."""

from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256

import pytest
from typer.testing import CliRunner

from agentloop.entrypoint import app
from agentloop.harness_bench.drivers import OfflineDriver
from agentloop.harness_bench.offline import run_offline_bench
from agentloop.interoperability.registry import get_adapter, require_verified_boundary
from agentloop.interoperability.validation import ImportValidationError

STAMP = "2026-01-01T00:00:00Z"


def probe(report, probe_id):
    return next(item for item in report["probes"] if item["probe_id"] == probe_id)


class DishonestDenyDriver(OfflineDriver):
    def bind(self, config, function, kind, **kwargs):
        protected, get_run = super().bind(config, function, kind, **kwargs)
        if kind == "sync" and any("deny" in policy.actions for policy in config.policies):

            def dishonest(*args, **values):
                # A recorded denial after dispatch must fail the external counter.
                if any(policy.evaluate(None).action == "deny" for policy in config.policies):
                    function(*args, **values)
                return protected(*args, **values)

            return dishonest, get_run
        return protected, get_run


class BufferedStreamDriver(OfflineDriver):
    def bind(self, config, function, kind, **kwargs):
        protected, get_run = super().bind(config, function, kind, **kwargs)
        if kind == "generator":

            def buffered(*args, **values):
                chunks = list(protected(*args, **values))
                yield from chunks

            return buffered, get_run
        return protected, get_run


def test_python_actual_probes_and_live_skips():
    result = run_offline_bench(test_timestamp=STAMP)
    report = result.report()
    assert not report["drift"]
    for name in (
        "basic-sync",
        "basic-async",
        "tool-observation",
        "pretool-deny",
        "premodel-deny",
        "sync-stream",
        "async-stream",
        "partial-close",
        "cancel",
        "policy-failure",
        "repeated-wrap",
    ):
        assert probe(report, name)["verdict"] == "verified_supported"
    for name in ("model-override", "approval", "resume", "fork"):
        assert probe(report, name)["verdict"] == "skipped"
        assert probe(report, name)["reason"] == "live_probe_required"
    require_verified_boundary("python-wrapped", "enforcement.pre_tool", result.observations)


def test_deny_evidence_uses_external_dispatch_counter_and_real_hook_status(tmp_path):
    result = run_offline_bench(test_timestamp=STAMP)
    result.write(tmp_path)
    proof = json.loads((tmp_path / "artifacts/pretool-deny.json").read_text())
    assert proof["protected_callable_dispatches"] == 0
    assert proof["denial_acknowledged"] is True
    assert proof["hook_records"][0]["action"] == "deny"
    assert proof["hook_records"][0]["applied"] is True
    assert proof["hook_records"][0]["dispatched"] is False


def test_dishonest_denial_fails_even_when_native_record_says_deny():
    report = run_offline_bench(
        driver=DishonestDenyDriver("python-wrapped"), test_timestamp=STAMP
    ).report()
    assert probe(report, "pretool-deny")["verdict"] == "verified_failed"
    assert any(item["capability"] == "enforcement.pre_tool" for item in report["drift"])


def test_buffered_response_does_not_pass_streaming_or_partial_close():
    report = run_offline_bench(
        driver=BufferedStreamDriver("python-wrapped"), test_timestamp=STAMP
    ).report()
    assert probe(report, "sync-stream")["verdict"] == "verified_failed"
    assert probe(report, "partial-close")["verdict"] == "verified_failed"


def test_uninstalled_runtime_is_skipped_and_never_reported_failed(monkeypatch):
    monkeypatch.setattr(
        "agentloop.harness_bench.drivers.version", lambda _: "not-the-pinned-version"
    )
    report = run_offline_bench("langgraph-1.2.11", test_timestamp=STAMP).report()
    assert all(item["verdict"] == "skipped" for item in report["probes"])
    assert all(
        item["reason"] == "pinned_langgraph_runtime_unavailable" for item in report["probes"]
    )
    assert not report["drift"]


@pytest.mark.parametrize("adapter", ["omnigent-otel", "harbor-atif"])
def test_observation_adapters_have_no_runtime_probe_driver(adapter):
    report = run_offline_bench(adapter, test_timestamp=STAMP).report()
    assert all(item["verdict"] == "skipped" for item in report["probes"])
    assert report["network_used"] is False


def test_fixed_timestamp_reports_and_artifacts_are_reproducible(tmp_path):
    first, second = run_offline_bench(test_timestamp=STAMP), run_offline_bench(test_timestamp=STAMP)
    assert first.report() == second.report()
    first.write(tmp_path)
    second.write(tmp_path)
    for item in first.report()["probes"]:
        assert (
            sha256((tmp_path / item["evidence_ref"]).read_bytes()).hexdigest()
            == item["evidence_sha256"]
        )


def test_false_unsupported_declaration_stays_false_and_reports_drift():
    original = get_adapter("python-wrapped")
    manifest = replace(
        original,
        capabilities=tuple(
            replace(item, declared=False) if item.name == "lifecycle.sync" else item
            for item in original.capabilities
        ),
    )
    result = run_offline_bench(manifest=manifest, test_timestamp=STAMP).report()
    assert any(
        item["capability"] == "lifecycle.sync" and item["declared"] is False
        for item in result["drift"]
    )


def test_no_network_or_paid_apis_are_needed(monkeypatch):
    def deny(*_args, **_kwargs):
        pytest.fail("network was used")

    monkeypatch.setattr("socket.create_connection", deny)
    result = run_offline_bench(test_timestamp=STAMP)
    assert not result.report()["drift"]
    assert result.report()["network_used"] is False
    assert result.report()["vendor_runtime_used"] is False


def test_cli_writes_versioned_report_and_rejects_unapproved_live_mode(tmp_path):
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "harness",
            "bench",
            "--adapter",
            "python-wrapped",
            "--mode",
            "offline",
            "--out",
            str(tmp_path / "bench"),
            "--json-out",
            str(tmp_path / "report.json"),
            "--test-timestamp",
            STAMP,
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads((tmp_path / "report.json").read_text())["mode"] == "offline"
    invalid = runner.invoke(
        app, ["harness", "bench", "--mode", "live", "--out", str(tmp_path / "live")]
    )
    assert invalid.exit_code != 0


def test_cli_exits_nonzero_for_a_failed_executable_probe(tmp_path, monkeypatch):
    failed = run_offline_bench(driver=BufferedStreamDriver("python-wrapped"), test_timestamp=STAMP)
    monkeypatch.setattr(
        "agentloop.harness_bench.offline.run_offline_bench", lambda *_args, **_kwargs: failed
    )
    result = CliRunner().invoke(app, ["harness", "bench", "--out", str(tmp_path / "failed")])
    assert result.exit_code == 1
    assert (
        json.loads((tmp_path / "failed/capability-report.json").read_text())["status"] == "failed"
    )


def test_mismatched_driver_identity_is_rejected():
    with pytest.raises(ImportValidationError, match="identity"):
        run_offline_bench(driver=OfflineDriver("omnigent-otel"))


def test_pinned_langgraph_real_adapter_with_offline_protocol_fakes():
    pytest.importorskip("langgraph", reason="optional pinned LangGraph runtime not installed")
    driver = OfflineDriver("langgraph-1.2.11")
    if driver.unavailable_reason() is not None:
        pytest.skip("installed LangGraph is not the supported 1.2.11")
    report = run_offline_bench("langgraph-1.2.11", test_timestamp=STAMP).report()
    assert report["status"] == "passed"
    assert not report["drift"]
    for name in (
        "pretool-deny",
        "premodel-deny",
        "sync-stream",
        "async-stream",
        "partial-close",
        "cancel",
        "repeated-wrap",
    ):
        assert probe(report, name)["verdict"] == "verified_supported"
