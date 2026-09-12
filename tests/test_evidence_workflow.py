from __future__ import annotations

import json
import runpy
from pathlib import Path


def test_end_to_end_evidence_artifacts_link_predictions_to_paired_outcomes(tmp_path):
    example = Path(__file__).resolve().parents[1] / "examples" / "intervention_study.py"
    workflow = runpy.run_path(str(example))
    summary = workflow["run"](tmp_path)
    assert summary["configured_gate_pass_rate"] == 5 / 6
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / "interventions").glob("*.json")
    ]
    assert {record["intervention_id"] for record in records} == set(summary["intervention_ids"])
    for record in records:
        prediction = record["predicted"]["findings"][0]
        assert prediction["estimate"]["calibrated"] is False
        assert prediction["finding_id"] in record["target_finding_ids"]
        assert record["measured"]["deltas"]["runtime_ms_delta"] < 0
    rejected = next(record for record in records if not record["gates_passed"])
    assert rejected["measured"]["quality"]["passed"] is False
    assert rejected["measured"]["deltas"]["cost_usd_delta"] is None
    study = json.loads((tmp_path / "study-results.json").read_text(encoding="utf-8"))
    assert study["comparisons"]["candidate"]["pair_count"] == 6
    assert (tmp_path / "example.html").is_file()
    assert workflow["run"](tmp_path) == summary
