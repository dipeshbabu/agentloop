"""Native study manifests plus complete external source population receipts."""

from __future__ import annotations

import json
from pathlib import Path

from agentloop.integrations.harbor.trial_evidence import PAIRING_KEYS
from agentloop.integrations.harbor.trials import HarborImportResult
from agentloop.interoperability.validation import ImportValidationError, indirect_path


def _write_stable(path: Path, value: dict) -> None:
    content = (json.dumps(value, indent=2) + "\n").encode("utf-8")
    if path.exists():
        if indirect_path(path) or path.read_bytes() != content:
            raise ImportValidationError(
                "output_conflict", "study", "existing study artifact conflicts"
            )
        return
    with path.open("xb") as stream:
        stream.write(content)


def write_harbor_study(
    conditions: dict[str, HarborImportResult],
    out: str | Path,
    *,
    baseline: str,
    name: str = "Harbor source study",
) -> Path:
    if (
        not isinstance(conditions, dict)
        or len(conditions) < 2
        or baseline not in conditions
        or any(not isinstance(key, str) or not key.strip() for key in conditions)
    ):
        raise ImportValidationError(
            "invalid_study",
            "conditions",
            "at least two explicit conditions and a baseline are required",
        )
    root = Path(out)
    root.mkdir(parents=True, exist_ok=True)
    manifests = {}
    population = {}
    for index, (condition, result) in enumerate(sorted(conditions.items())):
        folder = f"condition-{index}"
        result.write(root / folder)
        traces = [f"{folder}/traces/{trace.run_id}.json" for trace in result.trial_traces]
        if not traces:
            raise ImportValidationError(
                "invalid_study",
                "conditions",
                "a condition has no measurable source projections; retain its inventory separately",
            )
        manifests[condition] = traces
        population[condition] = {
            "inventory": f"{folder}/harbor-inventory.json",
            "trials_discovered": result.inventory()["trials_discovered"],
            "planned_unidentified_trials": result.inventory()["planned_unidentified_trials"],
            "native_study_trials": len(traces),
            "excluded_trial_rows": [
                row for row in result.inventory()["trial_rows"] if not row.get("trace_files")
            ],
            "interpretation": "native study statistics describe supplied native projections; complete population and missing trajectories remain in this inventory",
        }
    manifest = {
        "schema_version": "1.0",
        "name": name,
        "baseline": baseline,
        "conditions": manifests,
        "pairing_keys": list(PAIRING_KEYS),
    }
    path = root / "study.json"
    _write_stable(path, manifest)
    _write_stable(
        root / "cohort-inventory.json", {"schema_version": "1.0", "conditions": population}
    )
    return path
