"""Synthetic ranking declarations; no workload change or provider request."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agentloop.entrypoint import _analysis_payload, _quickstart_trace
from agentloop.html_report import analysis_to_html
from agentloop.ranking import RANKING_KEY


def declared(value, source, kind="declared"):
    return {"value": value, "kind": kind, "source_ref": source}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/finding-ranking"))
    out = parser.parse_args().out
    trace = _quickstart_trace()
    for event in trace.events:
        if event.event_type == "tool_call":
            event.metadata["parallel_safe"] = True
    trace.metadata[RANKING_KEY] = {
        "parallelize_tools": {
            "quality_risk": declared("medium", "synthetic-task-criteria:v1"),
            "reversible": declared(True, "synthetic-rollback:v1"),
            "validation_effort_minutes": declared(2, "synthetic-effort:v1"),
            "validation_cost_usd": declared(0, "synthetic-validation-cost:v1"),
            "estimated_cost_savings_usd": declared(0, "synthetic-cost-assumption:v1", "estimated"),
        }
    }
    payload = _analysis_payload(trace)
    out.mkdir(parents=True, exist_ok=True)
    (out / "analysis.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    (out / "analysis.html").write_text(analysis_to_html(payload), encoding="utf-8")
    print(
        json.dumps(
            {
                "synthetic": True,
                "ready_to_test": payload["diagnosis"]["ranking"]["ready_to_test_count"],
                "automatic_application_allowed": False,
            }
        )
    )


if __name__ == "__main__":
    main()
