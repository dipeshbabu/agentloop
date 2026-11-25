"""Workload-conditioned calibration (issue #403)."""

from __future__ import annotations

import pytest

from agentloop.conditioned_calibration import (
    CalibrationError,
    approve,
    evaluate,
    fit,
    predict,
)


def obs(
    pass_name="parallelize_tools", cluster="retrieval", predicted=1000.0, measured=500.0, **extra
):
    return {"pass": pass_name, "task_cluster": cluster, "predicted": predicted,
            "measured": measured, "success": True, **extra}  # fmt: skip


def overpredicting(clusters=("retrieval", "coding", "extraction"), per_cluster=6):
    # The pass consistently delivers about half of what it predicts.
    rows = []
    for cluster in clusters:
        for i in range(per_cluster):
            rows.append(
                obs(
                    cluster=cluster,
                    predicted=1000.0 + 100 * i,
                    measured=(1000.0 + 100 * i) * (0.45 + 0.02 * i),
                )
            )
    return rows


def test_fit_learns_a_ratio_with_an_interval_and_is_inactive():
    model = fit(overpredicting(), min_rows=5)
    assert model["active"] is False and model["review_ref"] is None
    pass_level = model["levels"][1]
    (factor,) = pass_level["factors"]
    assert factor["status"] == "fitted"
    assert 0.45 <= factor["ratio"] <= 0.56
    low, high = factor["interval"]
    assert low <= factor["ratio"] <= high


def test_prediction_requires_review():
    model = fit(overpredicting())
    with pytest.raises(CalibrationError, match="reviewed"):
        predict(model, {"pass": "parallelize_tools"}, 1000)
    approved = approve(model, "issue-403-review")
    assert model["active"] is False  # original untouched
    result = predict(approved, {"pass": "parallelize_tools", "task_cluster": "new"}, 1000)
    assert result["status"] == "calibrated" and result["reviewed"] is True
    assert result["stratum"] == {"pass": "parallelize_tools"}  # fell back to the coarser stratum
    with pytest.raises(CalibrationError):
        approve(model, " ")


def test_most_specific_stratum_wins():
    rows = overpredicting() + [obs(cluster="special", measured=2000.0) for _ in range(5)]
    model = approve(fit(rows), "r")
    special = predict(model, {"pass": "parallelize_tools", "task_cluster": "special"}, 1000)
    assert special["stratum"] == {"pass": "parallelize_tools", "task_cluster": "special"}
    assert special["prediction"] == 2000


def test_outside_coverage_abstains():
    model = approve(fit(overpredicting()), "r")
    assert predict(model, {"pass": "other_pass"}, 1000)["status"] == "abstained"
    assert predict(model, {"pass": "parallelize_tools"}, -5)["status"] == "abstained"


def test_small_cohorts_are_not_fitted():
    model = fit([obs() for _ in range(3)], min_rows=5)
    assert {f["status"] for level in model["levels"] for f in level["factors"]} == {
        "insufficient_rows"
    }


def test_estimated_missing_and_invalid_rows_are_counted_not_fitted():
    rows = overpredicting() + [
        obs(measured_basis="estimated"),
        obs(measured_basis="missing", measured=None),
        obs(predicted=0),
        obs(measured=float("nan")),
    ]
    counts = fit(rows)["counts"]
    assert counts == {"rows": 22, "not_observed": 3, "stale": 0, "invalid_prediction": 1}


def test_negative_results_and_confounders_are_retained():
    rows = overpredicting() + [obs(measured=-200.0, confounders=["cold cache"]) for _ in range(2)]
    factor = fit(rows)["levels"][1]["factors"][0]
    assert factor["negative_results"] == 2
    assert factor["with_confounders"] == 2
    assert factor["rows"] == 20


def test_stale_evidence_is_excluded():
    rows = [obs(observed_at="2026-01-01T00:00:00Z") for _ in range(5)] + [
        obs(observed_at="2026-11-20T00:00:00Z") for _ in range(5)
    ]
    model = fit(rows, as_of="2026-11-25T00:00:00Z", max_age_days=90)
    assert model["counts"]["stale"] == 5
    with pytest.raises(CalibrationError):
        fit(rows, max_age_days=90)


def test_held_out_clusters_beat_the_raw_heuristic():
    result = evaluate(overpredicting())
    assert result["split"] == "leave_one_task_cluster_out"
    assert result["calibrated"]["abstained"] == 0
    assert result["calibrated"]["mae"] < result["raw"]["mae"]
    assert result["calibrated"]["median_relative_error"] < result["raw"]["median_relative_error"]
    assert 0 <= result["calibrated"]["interval_coverage"] <= 1
    assert result["success_rate"] == 1.0


def test_held_out_evaluation_abstains_without_coverage():
    rows = overpredicting(clusters=("retrieval",)) + [obs(pass_name="rare", cluster="coding")]
    assert evaluate(overpredicting(clusters=("retrieval",)))["status"] == "abstained"
    result = evaluate(rows)
    assert result["calibrated"]["abstained"] >= 1


@pytest.mark.parametrize(
    "rows",
    [[{"task_cluster": "x"}], [obs(measured_basis="guessed")], ["not a mapping"]],
)
def test_malformed_observations_are_refused(rows):
    with pytest.raises(CalibrationError):
        fit(rows)


def test_settings_are_validated():
    with pytest.raises(CalibrationError):
        fit(overpredicting(), min_rows=1)
    with pytest.raises(CalibrationError):
        fit(overpredicting(), strata=[("tenant",)])


def test_fitting_is_deterministic():
    assert fit(overpredicting(), seed=4) == fit(overpredicting(), seed=4)
