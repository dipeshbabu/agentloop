"""Create interventions from project-scoped stored evidence without re-diagnosis."""

from __future__ import annotations

import math
from dataclasses import fields
from typing import Any

from agentloop.interventions import (
    InterventionReferenceError,
    InterventionValidationError,
    build_intervention,
)
from agentloop.quality import build_quality_report, validate_quality_fixtures
from agentloop.replay import ReplayGates


def parse_replay_gates(value: Any) -> ReplayGates:
    if not isinstance(value, dict):
        raise InterventionValidationError("gates must be an object")
    defaults = ReplayGates()
    names = {field.name for field in fields(defaults)}
    if set(value) - names:
        raise InterventionValidationError("unknown replay gate option")
    for key, item in value.items():
        if key.startswith("require_"):
            if type(item) is not bool:
                raise InterventionValidationError(f"{key} must be a boolean")
        elif item is None and key == "min_quality_score":
            continue
        elif (
            isinstance(item, bool)
            or not isinstance(item, int | float)
            or not math.isfinite(item)
            or item < 0
        ):
            raise InterventionValidationError(f"{key} must be a finite nonnegative number")
        elif key == "min_quality_score" and item > 1:
            raise InterventionValidationError("min_quality_score must be between 0 and 1")
    return ReplayGates(**value)


def create_stored_intervention(
    db: Any, request: dict[str, Any], project_id: str = "default"
) -> dict[str, Any]:
    allowed = {
        "baseline_run_id",
        "candidate_run_id",
        "target_finding_ids",
        "intervention_type",
        "configuration",
        "metadata",
        "gates",
        "quality_fixtures",
    }
    if not isinstance(request, dict) or set(request) - allowed:
        raise InterventionValidationError("invalid intervention request fields")
    for key in ("baseline_run_id", "candidate_run_id", "intervention_type"):
        if not isinstance(request.get(key), str) or not request[key].strip():
            raise InterventionValidationError(f"{key} must be a nonempty string")
    targets = request.get("target_finding_ids")
    if (
        not isinstance(targets, list)
        or not targets
        or any(not isinstance(item, str) or not item for item in targets)
    ):
        raise InterventionValidationError("target_finding_ids must contain at least one ID")
    gates = parse_replay_gates(request.get("gates", {}))
    baseline = db.get_trace(request["baseline_run_id"], project_id=project_id)
    candidate = db.get_trace(request["candidate_run_id"], project_id=project_id)
    if baseline is None or candidate is None:
        raise InterventionReferenceError("baseline or candidate trace not found")
    snapshots = [
        db.get_finding_snapshot(baseline.run_id, target, project_id=project_id)
        for target in sorted(set(targets))
    ]
    if any(snapshot is None for snapshot in snapshots):
        raise InterventionReferenceError("target baseline finding not found")
    fixtures = request.get("quality_fixtures")
    if fixtures is not None:
        validate_quality_fixtures(fixtures)
        if any(
            str(fixture.get("scorer", {}).get("type", "")).strip().lower() == "custom"
            for fixture in fixtures
        ):
            raise InterventionValidationError(
                "custom Python scorers are not accepted for stored intervention requests"
            )
    quality = (
        None
        if fixtures is None
        else build_quality_report(
            fixtures,
            baseline_trace=baseline,
            candidate_trace=candidate,
            min_score=gates.min_quality_score,
        )
    )
    record = build_intervention(
        baseline,
        candidate,
        target_finding_ids=targets,
        intervention_type=request["intervention_type"],
        configuration=request.get("configuration", {}),
        metadata=request.get("metadata", {}),
        diagnosis={"run_id": baseline.run_id, "findings": snapshots},
        gates=gates,
        quality_report=quality,
    )
    return db.save_intervention(record, project_id=project_id)
