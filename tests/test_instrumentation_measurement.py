from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import measure_instrumentation as measurement


def test_counterbalanced_controls_retain_phases_and_native_evidence(tmp_path):
    root = tmp_path / "measurement"
    report = measurement.run(root, source_revision="0" * 40, sizes=(3,), repetitions=2)
    assert [row["order"] for row in report["attempts"]] == [[True, False], [False, True]]
    assert all(row["qualified"] and row["output"] == 5 for row in report["attempts"])
    summary = report["summaries"][0]
    assert summary["qualified_pairs"] == summary["planned_pairs"] == 2
    for phase in ("traced_ms", "untraced_ms", "export_ms", "analysis_ms", "difference_ms"):
        assert summary[phase]["count"] == 2
    assert report["protocol"]["cost_usd"] is None
    assert len(list(root.glob("*/trace.json"))) == 2
    with pytest.raises(FileExistsError):
        measurement.run(root, source_revision="0" * 40, sizes=(3,), repetitions=2)


def test_measurement_failures_remain_in_the_denominator(tmp_path, monkeypatch):
    def fail(operation):
        raise RuntimeError("owned failure")

    monkeypatch.setattr(measurement, "timed", fail)
    report = measurement.run(
        tmp_path / "failed", source_revision="0" * 40, sizes=(2,), repetitions=2
    )
    assert len(report["attempts"]) == 2
    assert all(row["status"] == "failed" for row in report["attempts"])
    assert report["summaries"][0]["difference_ms"]["missing_count"] == 2


def test_cancelled_measurement_preserves_protocol_and_attempt(tmp_path, monkeypatch):
    def cancel(operation):
        raise KeyboardInterrupt

    monkeypatch.setattr(measurement, "timed", cancel)
    root = tmp_path / "cancelled"
    with pytest.raises(KeyboardInterrupt):
        measurement.run(root, source_revision="0" * 40, sizes=(2,), repetitions=2)
    assert len(json.loads((root / "protocol.json").read_bytes())["planned_pairs"]) == 2
    attempt = json.loads((root / "spans-2-repeat-0/attempt.json").read_bytes())
    assert attempt["status"] == "cancelled" and attempt["qualified"] is False


@pytest.mark.parametrize(
    "settings", [{"repetitions": 1}, {"repetitions": True}, {"sizes": (0,)}, {"sizes": (2, 2)}]
)
def test_invalid_protocol_does_not_create_output(tmp_path, settings):
    root = tmp_path / "invalid"
    with pytest.raises(ValueError):
        measurement.run(root, source_revision="0" * 40, **settings)
    assert not root.exists()


def test_retained_controls_preserve_all_native_attempts(tmp_path):
    from agentloop.studies import summarize_values
    from agentloop.tracer import AgentTrace
    from examples.real_agent_study.archive import unpack

    index = Path(__file__).resolve().parents[1] / "research/instrumentation-2026-11/index.json"
    assert unpack(index, tmp_path) == 118
    for label in ("first-controls", "final-controls"):
        root = tmp_path / label
        report = json.loads((root / "summary.json").read_bytes())
        assert len(report["attempts"]) == 18
        for row in report["attempts"]:
            case = root / f"spans-{row['size']}-repeat-{row['repetition']}"
            assert json.loads((case / "attempt.json").read_bytes()) == row
            trace = AgentTrace.from_json(case / "trace.json")
            assert trace.metadata["synthetic"] is True
            assert trace.metadata["output"] == row["expected"] == row["output"]
            assert len(trace.events) == row["size"]
            assert row["qualified"] and row["cost_usd"] is None
        for summary in report["summaries"]:
            differences = [
                row["difference_ms"]
                for row in report["attempts"]
                if row["size"] == summary["spans"]
            ]
            assert summarize_values(differences) == summary["difference_ms"]
