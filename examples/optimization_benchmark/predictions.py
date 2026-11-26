"""Post-hoc: AgentLoop's predicted savings versus the measured held-out change.

Not part of the frozen protocol. The frozen runner read the wrong field for a
finding's predicted saving (it recorded 0), so this script recomputes the
prediction from the saved development traces with the same AgentLoop code and
compares it with the measured change of the method that applied the finding.

    python -m examples.optimization_benchmark.predictions research/optimization-benchmark-2026-11
"""

from __future__ import annotations

import json
import statistics
import sys
import zipfile
from pathlib import Path

from agentloop.findings import build_diagnosis
from agentloop.tracer import AgentTrace


def predictions(evidence: Path, finding_type: str = "route_to_smaller_model") -> dict:
    archive = zipfile.ZipFile(evidence / "traces.zip")
    rows = json.loads((evidence / "held-out.json").read_text(encoding="utf-8"))
    by = {(r["family"], r["task"], r["method"]): r for r in rows}
    out = {}
    for family in ("math", "extraction", "coding"):
        fractions = []
        for name in archive.namelist():
            if not name.startswith(f"traces/dev-baseline/{family}/"):
                continue
            trace = AgentTrace.from_dict(json.loads(archive.read(name)))
            found = [
                f for f in build_diagnosis(trace).get("findings", []) if f["type"] == finding_type
            ]
            saving = sum(f["savings"]["estimated_latency_savings_ms"] or 0 for f in found)
            fractions.append(saving / (trace.elapsed_ms or 1))
        measured = [
            (
                by[(family, task, "none")]["runtime_ms"]
                - by[(family, task, "heuristic")]["runtime_ms"]
            )
            / by[(family, task, "none")]["runtime_ms"]
            for (f, task, method) in by
            if f == family and method == "none"
        ]
        out[family] = {
            "finding": finding_type,
            "predicted_saving_fraction_median": round(statistics.median(fractions), 3),
            "measured_saving_fraction_median": round(statistics.median(measured), 3),
            "evidence": "prediction from development traces; measurement on held-out tasks",
        }
    return out


if __name__ == "__main__":
    print(json.dumps(predictions(Path(sys.argv[1])), indent=2))
