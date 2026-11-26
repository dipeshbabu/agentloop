"""Capture and freeze a task set, then compare a developer-applied change.

The stages live in ``improvement_capture``, ``improvement_planning``,
``improvement_decision`` and ``improvement_compare`` (issue #407). Every name
is re-exported here.
"""

from __future__ import annotations

from agentloop.experiment_artifacts import write_once
from agentloop.improvement_capture import (
    CONTROL_CAPTURE,
    DECISIONS,
    PERFORMANCE_GATES,
    SCORERS,
    VERSION,
    capture,
    load_json,
    now,
    read_capture,
    read_workload,
    require,
    slots,
    trace_receipt,
    validate_workload,
)
from agentloop.improvement_capture import _invoke as _invoke
from agentloop.improvement_capture import _stop_process as _stop_process
from agentloop.improvement_compare import (
    COMPARE_LAYOUT,
    compare,
)
from agentloop.improvement_decision import (
    finish,
)
from agentloop.improvement_planning import (
    MIN_SIGN_TEST_PAIRS,
    NOISY_SPREAD_PCT,
    planning_guidance,
    start,
)

__all__ = [
    "COMPARE_LAYOUT",
    "CONTROL_CAPTURE",
    "DECISIONS",
    "MIN_SIGN_TEST_PAIRS",
    "NOISY_SPREAD_PCT",
    "PERFORMANCE_GATES",
    "SCORERS",
    "VERSION",
    "capture",
    "compare",
    "finish",
    "load_json",
    "now",
    "planning_guidance",
    "read_capture",
    "read_workload",
    "require",
    "slots",
    "start",
    "trace_receipt",
    "validate_workload",
    "write_once",
]
