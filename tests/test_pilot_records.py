"""Developer pilot records and method comparisons (part of issue #406)."""

from __future__ import annotations

import pytest

from agentloop.pilot_records import (
    METHOD_SCHEMA,
    PILOT_SCHEMA,
    RecordError,
    compare_methods,
    summarize_pilot,
    validate_pilot_record,
)


def pilot(participant="p01", **overrides):
    record = {
        "schema": PILOT_SCHEMA,
        "participant": participant,
        "consent_ref": "consent-log#1",
        "application_category": "retrieval",
        "agentloop_version": "0.2.10",
        "started_at": "2026-12-01T10:00:00+00:00",
        "first_valid_trace_at": "2026-12-01T10:12:00+00:00",
        "first_useful_finding_at": "2026-12-01T10:30:00+00:00",
        "outcome": "accepted",
        "outcome_at": "2026-12-01T11:00:00+00:00",
        "failed_steps": [],
        "assistance": [],
        "effort_minutes": 60,
    }
    record.update(overrides)
    return record


def test_pilot_record_derives_minutes():
    checked = validate_pilot_record(pilot())
    assert checked["minutes_to_first_valid_trace"] == 12
    assert checked["minutes_to_first_useful_finding"] == 30
    assert checked["minutes_to_outcome"] == 60


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"email": "a@example.com"}, "personal"),
        ({"Name": "Ada"}, "personal"),
        ({"consent_ref": ""}, "consent"),
        ({"participant": "has spaces"}, "participant"),
        ({"first_valid_trace_at": "2026-12-01T09:00:00+00:00"}, "before started_at"),
        ({"first_useful_finding_at": "2026-12-01T10:05:00+00:00"}, "before first_valid_trace_at"),
        ({"outcome": "great"}, "outcome must be"),
        ({"outcome_at": None}, "needs outcome_at"),
        ({"failed_steps": "none"}, "failed_steps"),
        ({"effort_minutes": -3}, "effort_minutes"),
        ({"started_at": "yesterday"}, "ISO timestamp"),
        ({"schema": "other"}, "schema"),
    ],
)
def test_invalid_pilot_records_are_refused(overrides, fragment):
    with pytest.raises(RecordError, match=fragment):
        validate_pilot_record(pilot(**overrides))


def test_summary_keeps_denominators_and_negative_outcomes():
    records = [
        pilot("p01"),
        pilot("p02", outcome="rejected", assistance=["installed the wrong extra"]),
        pilot(
            "p03",
            outcome="abandoned",
            first_useful_finding_at=None,
            outcome_at=None,
            failed_steps=["no trace written"],
        ),  # fmt: skip
        pilot("p04", outcome="in_progress", outcome_at=None),
    ]
    summary = summarize_pilot(records)
    assert summary["participants"] == 4
    assert summary["first_useful_finding"] == {"median": 30, "reached": 3, "of": 4}
    assert summary["acceptance_rate"] == {"value": 0.5, "accepted": 1, "of_decided": 2}
    assert summary["outcomes"]["abandoned"] == 1
    assert summary["needed_assistance"] == 1 and summary["with_failed_steps"] == 1


def test_summary_of_nothing_and_duplicates():
    empty = summarize_pilot([])
    assert empty["participants"] == 0 and empty["acceptance_rate"]["value"] is None
    with pytest.raises(RecordError, match="once"):
        summarize_pilot([pilot("p01"), pilot("p01")])


def result(workload, method, kind, runtime, quality=0.9, **extra):
    return {"schema": METHOD_SCHEMA, "workload": workload, "method": method, "method_kind": kind,
            "split": "held_out", "negative_control": False, "quality_source": "independent",
            "quality": quality, "runtime_ms": runtime, "cost_usd": None, **extra}  # fmt: skip


def benchmark():
    rows = []
    for workload, base in (("code", 1000), ("search", 2000), ("extract", 1500)):
        rows.append(result(workload, "none", "no_optimization", base))
        rows.append(result(workload, "heuristic", "heuristic_ranking", base - 100))
        rows.append(result(workload, "reuse_pass", "agentloop_pass", base - 400))
    rows.append(result("control", "none", "no_optimization", 1000, negative_control=True))
    rows.append(result("control", "reuse_pass", "agentloop_pass", 700, negative_control=True))
    rows.append(result("control", "heuristic", "heuristic_ranking", 1000, negative_control=True))
    return rows


def test_methods_are_compared_with_the_baseline_on_held_out_workloads():
    report = compare_methods(benchmark())
    by_method = {m["method"]: m for m in report["methods"]}
    assert report["held_out_workloads"] == 4
    assert by_method["reuse_pass"]["median_runtime_gain_ms"] == 400
    assert by_method["heuristic"]["median_runtime_gain_ms"] == 100
    assert by_method["reuse_pass"]["workloads"] == 3
    # Improving a negative control signals noise or leakage.
    assert by_method["reuse_pass"]["negative_controls_improved"] == 1
    assert by_method["heuristic"]["negative_controls_improved"] == 0


def test_quality_regressions_unknown_costs_and_exclusions():
    rows = benchmark() + [
        result("code", "small_model", "agentloop_pass", 500, quality=0.5),
        result("code", "dev_only", "manual", 100, split="development"),
        result("code", "claimed", "manual", 100, quality_source="self_reported"),
        result("orphan", "reuse_only", "agentloop_pass", 100),
    ]
    report = compare_methods(rows)
    by_method = {m["method"]: m for m in report["methods"]}
    assert by_method["small_model"]["quality_regressions"] == 1
    assert by_method["reuse_pass"]["cost_known"] == 0
    assert report["development_rows_excluded"] == 1
    assert report["self_reported_rows_excluded"] == 1
    assert by_method["reuse_only"]["without_baseline"] == 1
    assert "dev_only" not in by_method and "claimed" not in by_method


@pytest.mark.parametrize(
    "overrides",
    [
        {"method_kind": "magic"},
        {"split": "train"},
        {"negative_control": "no"},
        {"quality_source": "vibes"},
        {"quality": None},
        {"runtime_ms": -1},
        {"schema": "x"},
    ],
)
def test_invalid_method_results_are_refused(overrides):
    with pytest.raises(RecordError):
        compare_methods([{**result("w", "m", "manual", 1), **overrides}])


def test_duplicate_held_out_results_are_refused():
    with pytest.raises(RecordError, match="one held-out result"):
        compare_methods([result("w", "m", "manual", 1), result("w", "m", "manual", 2)])
