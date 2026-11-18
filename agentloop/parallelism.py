"""Conservative concurrency candidates shared by reports and execution graphs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from agentloop.operations import operation_kind
from agentloop.timing import duration_ms, event_interval_ms
from agentloop.workflow_types import stage_summary

PARALLELISM_NOTICE = (
    "The tool name repeats in the same scope. Compare inputs and check dependencies "
    "and shared state before changing the calls."
)
PARALLELISM_REWRITE = (
    "After verifying output independence and shared-state safety, use asyncio.gather "
    "or ThreadPoolExecutor and validate the change with replay."
)
PARALLELISM_FORMULA = (
    "sum(tool durations) - max(tool duration), assuming the calls can run concurrently"
)
REUSE_REWRITE = (
    "Test reusing the first result within the same request. Confirm that the key covers "
    "all effective inputs, the source stays stable, and task checks still pass."
)
REUSE_FORMULA = "sum_duration_ms - retained_duration_ms"
_REUSE_ASSUMPTIONS = (
    "The declared key covers all effective inputs and the source stays stable within the request.",
    "Reused results preserve required behavior and add negligible overhead.",
    "The skipped calls are on the runtime-limiting path.",
)
_ASSUMPTIONS = (
    "No call consumes another call's output.",
    "Calls do not conflict through shared mutable state.",
    "Calls are on the runtime-limiting path and scheduling adds no overhead.",
)


def parallelization_candidates(
    events: Iterable[Any],
    *,
    dependency_edges: Iterable[tuple[str, str]] = (),
    consider_reuse: bool = False,
) -> list[dict[str, Any]]:
    """Return hypotheses, never proof of independence or verified savings.

    Optional metadata: ``parallel_safe`` is a boolean declaration, ``depends_on``
    is a list of span IDs. Explicit relationships take precedence over declarations.
    Inferred sequence edges must not be passed as causal dependencies.
    """
    by_id = {}
    groups: dict[tuple[str, str | None], list[str]] = {}
    dependencies: dict[str, set[str]] = {}
    invalid_dependencies: set[str] = set()
    unsafe: set[str] = set()
    declarations: set[str] = set()
    reuse_keys: dict[str, str] = {}
    for index, event in enumerate(events):
        span_id = (
            getattr(event, "event_id", None) or getattr(event, "node_id", None) or f"node_{index}"
        )
        if span_id in by_id:
            return []  # Ambiguous span identity cannot support dependency analysis.
        by_id[span_id] = event
        metadata = getattr(event, "metadata", None)
        metadata = metadata if isinstance(metadata, Mapping) else {}
        required = metadata.get("depends_on", [])
        if not isinstance(required, list) or any(
            not isinstance(value, str) or not value for value in required
        ):
            invalid_dependencies.add(span_id)
            required = []
        dependencies[span_id] = set(required)
        stage = stage_summary(event)
        if stage is not None and (
            stage.get("schema_status") != "supported" or stage.get("dependency_status") == "invalid"
        ):
            invalid_dependencies.add(span_id)
        if "parallel_safe" in metadata:
            if metadata["parallel_safe"] is True:
                declarations.add(span_id)
            else:
                unsafe.add(span_id)
        if operation_kind(event) == "tool":
            groups.setdefault((event.name, getattr(event, "parent_id", None)), []).append(span_id)
            reuse_key = metadata.get("tool_reuse_key")
            if (
                consider_reuse
                and metadata.get("read_only") is True
                and getattr(event, "status", None) == "ok"
                and isinstance(reuse_key, str)
                and 0 < len(reuse_key) <= 128
                and reuse_key == reuse_key.strip()
            ):
                reuse_keys[span_id] = reuse_key
    for span_id, event in by_id.items():
        parent_id = getattr(event, "parent_id", None)
        if parent_id in by_id:
            dependencies[span_id].add(parent_id)
    for source, target in dependency_edges:
        if target in dependencies:
            dependencies[target].add(source)

    candidates = []
    for (name, _), span_ids in groups.items():
        members = set(span_ids)
        if len(members) < 3 or members & unsafe or members & invalid_dependencies:
            continue
        pending = [ancestor for member in members for ancestor in dependencies[member]]
        visited = set()
        blocked = False
        while pending:
            ancestor = pending.pop()
            if ancestor in members or ancestor not in by_id or ancestor in invalid_dependencies:
                blocked = True
                break
            if ancestor not in visited:
                visited.add(ancestor)
                pending.extend(dependencies[ancestor])
        if blocked:
            continue

        intervals = [event_interval_ms(by_id[span_id]) for span_id in span_ids]
        known = sorted(interval for interval in intervals if interval is not None)
        if any(previous[1] > current[0] for previous, current in zip(known, known[1:])):
            continue  # Observed overlap already invalidates the serial-savings model.
        complete_timing = len(known) == len(span_ids)
        durations = [duration_ms(by_id[span_id]) for span_id in span_ids]
        sequential, parallel = sum(durations), max(durations)
        if sequential <= parallel:
            continue
        keys = [reuse_keys.get(span_id) for span_id in span_ids]
        if complete_timing and keys[0] is not None and all(key == keys[0] for key in keys):
            retained = min(range(len(intervals)), key=intervals.__getitem__)
            candidates.append(
                {
                    "name": name,
                    "event_type": "tool_call",
                    "count": len(span_ids),
                    "node_ids": sorted(span_ids),
                    "sequential_time_ms": round(sequential, 3),
                    "estimated_savings_ms": round(sequential - durations[retained], 3),
                    "evidence_level": "declared",
                    "confidence": "medium",
                    "assumptions": list(_REUSE_ASSUMPTIONS),
                    "observations": {
                        "evidence_level": "observed",
                        "tool_call_count": len(span_ids),
                        "cumulative_span_time_ms": round(sequential, 3),
                        "timing": "non_overlapping",
                        "recommended_action": "test_request_local_reuse",
                        "same_input_key_declared": True,
                        "read_only_declared": True,
                        "retained_span_id": span_ids[retained],
                    },
                    "description": "The integration declares matching input keys and read-only behavior for these successful calls.",
                    "estimate_formula": REUSE_FORMULA,
                }
            )
            continue
        declared = members <= declarations
        assumptions = list(_ASSUMPTIONS)
        if not complete_timing:
            assumptions.append("Calls ran serially; timing is incomplete or inconsistent.")
        candidates.append(
            {
                "name": name,
                "event_type": "tool_call",
                "count": len(span_ids),
                "node_ids": sorted(span_ids),
                "sequential_time_ms": round(sequential, 3),
                "estimated_parallel_time_ms": round(parallel, 3),
                "estimated_savings_ms": round(sequential - parallel, 3),
                "evidence_level": "declared" if declared else "inferred",
                "confidence": "medium" if declared and complete_timing else "low",
                "assumptions": assumptions,
                "observations": {
                    "evidence_level": "observed",
                    "tool_call_count": len(span_ids),
                    "cumulative_span_time_ms": round(sequential, 3),
                    "timing": "non_overlapping" if complete_timing else "unavailable",
                    "tool_name": name,
                    "recommended_action": "test_parallelization"
                    if declared
                    else "inspect_repetition",
                },
                "description": (
                    "The integration declares these calls safe to run concurrently; validate with replay."
                    if declared
                    else PARALLELISM_NOTICE
                ),
                "estimate_formula": PARALLELISM_FORMULA,
            }
        )
    return sorted(
        candidates, key=lambda item: (-item["estimated_savings_ms"], item["name"], item["node_ids"])
    )
