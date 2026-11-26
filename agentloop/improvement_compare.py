"""Run capture, planning and the decision as one interleaved comparison.

Split out of ``agentloop.improvement`` (issue #407), which re-exports every
name, so existing imports keep working.
"""

from __future__ import annotations

import math
from pathlib import Path

from agentloop.experiment_artifacts import local_path
from agentloop.improvement_capture import (
    DECISIONS,
    capture,
    read_workload,
    require,
)
from agentloop.improvement_decision import (
    finish,
)
from agentloop.improvement_planning import (
    _runner_budget,
    start,
)
from agentloop.improvement_verifier import DEFAULT_TIMEOUT, LocalVerifier

COMPARE_LAYOUT = {
    "baseline": "baseline",
    "session": "session",
    "paired": "paired",
    "results": "results",
}


def compare(
    workload_path,
    root,
    before,
    after,
    *,
    change,
    timeout=180,
    show_output=False,
    focus=None,
    objective="latency",
    minimum_improvement=0,
    decision="aggregate",
    verifier=None,
    verifier_timeout=DEFAULT_TIMEOUT,
    max_runner_regression_ms=None,
    progress=None,
):
    """Run the whole guided comparison for an unchanged and a changed runner.

    The ``before`` runner is captured as the baseline, the session is frozen,
    and the ``after`` runner is then captured with ``before`` as an
    interleaved control. ``before`` must therefore stay runnable and unchanged,
    for example from a separate git worktree. Every stage writes its usual
    artifacts under ``root`` (see ``COMPARE_LAYOUT``), so a failed run can be
    inspected or resumed with the individual commands.
    """
    notify = progress or (lambda message: None)
    # Check cheap inputs before any runner executes, so a typo cannot waste a
    # full baseline capture.
    require(isinstance(change, str) and bool(change.strip()), "Describe the change being compared")
    for argv, side in ((before, "before"), (after, "after")):
        require(
            isinstance(argv, (list, tuple))
            and argv
            and all(isinstance(a, str) and a for a in argv),
            f"Supply an explicit {side} runner command",
        )
    require(list(before) != list(after), "The before and after runners must differ")
    require(objective in {"latency", "cost"}, "Objective must be latency or cost")
    decision = decision.replace("-", "_") if isinstance(decision, str) else decision
    require(decision in DECISIONS, "Decision must be per-pair or aggregate")
    _runner_budget(max_runner_regression_ms)
    require(
        type(minimum_improvement) in (int, float)
        and math.isfinite(minimum_improvement)
        and minimum_improvement >= 0,
        "Minimum improvement must be finite and nonnegative",
    )
    if verifier is not None:
        LocalVerifier.read(verifier, verifier_timeout)
    read_workload(workload_path)
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    paths = {name: local_path(root, relative) for name, relative in COMPARE_LAYOUT.items()}

    notify("Capturing the baseline with the before runner")
    baseline = capture(
        workload_path, paths["baseline"], list(before), timeout=timeout, show_output=show_output
    )
    completed = sum(row["status"] == "completed" for row in baseline["runs"])
    require(
        completed > 0,
        "No baseline attempt completed. Fix the before runner, using "
        f"{paths['baseline'] / 'capture.json'} and --show-runner-output, then rerun.",
    )
    notify(
        f"Baseline captured {completed} of {len(baseline['runs'])} attempts; freezing the session"
    )
    specification = start(
        workload_path,
        paths["baseline"] / "capture.json",
        paths["session"],
        focus=focus,
        objective=objective,
        minimum_improvement=minimum_improvement,
        decision=decision,
        verifier=verifier,
        verifier_timeout=verifier_timeout,
        max_runner_regression_ms=max_runner_regression_ms,
    )
    for note in specification["planning_notes"]:
        notify(f"Note: {note['message']}")
    notify("Capturing the after runner, alternating with the before runner as a control")
    capture(
        workload_path,
        paths["paired"],
        list(after),
        timeout=timeout,
        show_output=show_output,
        control=list(before),
    )
    notify("Comparing every planned pair")
    return finish(
        paths["session"] / "session.json",
        paths["paired"] / "capture.json",
        paths["results"],
        change=change,
        verifier=verifier,
    )
