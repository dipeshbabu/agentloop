"""Contract for agentloop.improvement's names while it is split into stages (#407)."""

from __future__ import annotations

import importlib

import pytest

import agentloop.improvement as improvement

HOMES = {
    "improvement_capture": [
        "CONTROL_CAPTURE", "DECISIONS", "PERFORMANCE_GATES", "SCORERS", "VERSION",
        "capture", "load_json", "now", "read_capture", "read_workload", "require",
        "slots", "trace_receipt", "validate_workload", "_invoke", "_stop_process",
    ],
    "improvement_planning": ["MIN_SIGN_TEST_PAIRS", "NOISY_SPREAD_PCT", "planning_guidance", "start"],
    "improvement_decision": ["finish"],
    "improvement_compare": ["COMPARE_LAYOUT", "compare"],
    "experiment_artifacts": ["write_once"],
}  # fmt: skip


@pytest.mark.parametrize("module", sorted(HOMES))
def test_names_resolve_to_the_same_objects(module):
    home = importlib.import_module(f"agentloop.{module}")
    for name in HOMES[module]:
        assert getattr(improvement, name) is getattr(home, name), name


def test_all_lists_every_public_name():
    public = {n for names in HOMES.values() for n in names if not n.startswith("_")}
    assert set(improvement.__all__) == public


def test_the_cli_still_reaches_every_stage_through_improvement():
    # improvement_cli calls improvement.capture/start/finish/compare at call
    # time, so code that patches them on agentloop.improvement still works.
    import inspect

    from agentloop import improvement_cli

    source = inspect.getsource(improvement_cli)
    for name in ("capture", "start", "finish", "compare"):
        assert f"improvement.{name}(" in source
