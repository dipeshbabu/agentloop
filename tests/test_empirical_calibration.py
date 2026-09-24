from __future__ import annotations

import json
from pathlib import Path

from agentloop.calibration import summarize_calibration
from examples.empirical_calibration import Inventory, archive_time
from examples.real_agent_study.archive import unpack


def test_relocated_archive_times_are_not_backdated(tmp_path):
    path = tmp_path / "prediction.json"
    path.write_text("{}")
    assert archive_time(path, before="2000-01-01T00:00:00+00:00") is None
    assert archive_time(path) is not None


def test_complete_original_inventory_keeps_combined_ledgers_unattributed(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "original"
    unpack(root / "research/real-agent-2026-09/index.json", source)
    inventory = Inventory(tmp_path / "calibration")
    inventory.routing_study(source / "study")
    assert len(inventory.registrations) == 168
    selected = [case for case in inventory.registrations if case["selection"] == "selected"]
    rejected = [case for case in inventory.registrations if case["selection"] == "rejected"]
    assert len(selected) == 98 and len(rejected) == 70
    assert all(case["outcome"] is None for case in rejected)
    # Extracting a published bundle does not recover its original write times.
    assert all(case["prediction_recorded_at"] is None for case in selected)
    for case in selected:
        record = json.loads((inventory.out / case["outcome"]["record"]).read_text())
        assert len(record["predicted"]["findings"]) > 1
        assert case["prediction"] in record["predicted"]["findings"]
    report = inventory.finish()
    assert report["registration_count"] == 168
    assert report["runtime_coefficients_changed"] is False
    assert all(
        metric["fit"]["factor"] is None
        for cohort in report["cohorts"]
        for metric in cohort["metrics"].values()
    )
    repeated = summarize_calibration(inventory.out / "manifest.json")
    assert repeated == report
