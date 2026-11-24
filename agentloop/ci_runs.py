"""Group traces uploaded by CI into runs and pair each run's baseline and candidate.

The reusable comparison workflow labels uploaded traces with ``workflow_run``,
``condition`` (``baseline`` or ``candidate``), ``commit``, ``ref`` and
``repository``. These helpers read those labels back so a run's comparison can
be opened without picking traces by hand.
"""

from __future__ import annotations

from typing import Any

from agentloop.store import MAX_PAGE_SIZE
from agentloop.tracer import AgentTrace

RUN_LABEL = "workflow_run"
CONDITION_LABEL = "condition"
CONDITIONS = ("baseline", "candidate")
CONTEXT_LABELS = ("repository", "commit", "ref")
DEFAULT_SCAN = 500


class CIRunNotFoundError(LookupError):
    """The project has no complete baseline/candidate pair for a CI run."""


def _pages(store, project_id: str, *, label=None, scan: int = DEFAULT_SCAN):
    cursor = None
    seen = 0
    while seen < scan:
        page = store.list_traces_page(
            project_id=project_id,
            limit=min(MAX_PAGE_SIZE, scan - seen),
            cursor=cursor,
            label=label,
        )
        yield from page["items"]
        seen += len(page["items"])
        cursor = page["next_cursor"]
        if not cursor or not page["items"]:
            return


def list_ci_runs(store, project_id: str, *, scan: int = DEFAULT_SCAN) -> list[dict[str, Any]]:
    """CI runs among a project's newest ``scan`` traces, newest run first.

    Each run lists the newest baseline and candidate run IDs it uploaded (or
    ``None``), its repository, commit and ref, and whether it can be compared.
    """
    if not isinstance(scan, int) or isinstance(scan, bool) or scan < 1:
        raise ValueError("scan must be a positive number of traces")
    runs: dict[str, dict[str, Any]] = {}
    for item in _pages(store, project_id, scan=scan):
        labels = item.get("labels") or {}
        run = labels.get(RUN_LABEL)
        if not isinstance(run, str) or not run:
            continue
        entry = runs.setdefault(
            run,
            {
                RUN_LABEL: run,
                **{name: None for name in CONTEXT_LABELS},
                "baseline_run_id": None,
                "candidate_run_id": None,
                "latest_upload": item.get("created_at"),
                "traces": 0,
            },
        )
        entry["traces"] += 1
        for name in CONTEXT_LABELS:
            if entry[name] is None and isinstance(labels.get(name), str):
                entry[name] = labels[name]
        condition = labels.get(CONDITION_LABEL)
        if condition in CONDITIONS and entry[f"{condition}_run_id"] is None:
            # Listings are newest first, so the first match is the newest upload.
            entry[f"{condition}_run_id"] = item["run_id"]
    for entry in runs.values():
        entry["comparable"] = bool(entry["baseline_run_id"] and entry["candidate_run_id"])
    return list(runs.values())


def ci_run_pair(
    store, project_id: str, workflow_run: str, *, scan: int = DEFAULT_SCAN
) -> tuple[AgentTrace, AgentTrace]:
    """Load the newest baseline and candidate traces uploaded by one CI run."""
    found: dict[str, str] = {}
    for item in _pages(store, project_id, label=(RUN_LABEL, workflow_run), scan=scan):
        condition = (item.get("labels") or {}).get(CONDITION_LABEL)
        if condition in CONDITIONS and condition not in found:
            found[condition] = item["run_id"]
        if len(found) == len(CONDITIONS):
            break
    missing = [condition for condition in CONDITIONS if condition not in found]
    if missing:
        raise CIRunNotFoundError(
            f"CI run {workflow_run} has no {' or '.join(missing)} trace in this project"
        )
    traces = []
    for condition in CONDITIONS:
        trace = store.get_trace(run_id=found[condition], project_id=project_id)
        if trace is None:
            raise CIRunNotFoundError(f"The {condition} trace of CI run {workflow_run} was deleted")
        traces.append(trace)
    return traces[0], traces[1]
