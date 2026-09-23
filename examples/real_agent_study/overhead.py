"""Descriptive tracing overhead on matched pilot executions, including failures."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "real_agent_study"

from agentloop.studies import summarize_values

from .results import case_path, read_case, selected_tasks
from .run import fingerprint, load_protocol, write_new


def summarize(protocol_path, root):
    root = Path(root)
    protocol = load_protocol(protocol_path, root / "sources")
    rows = []
    for task in selected_tasks(protocol, "pilot"):
        pair = [
            read_case(
                case_path(root, "pilot", "baseline", 0, task["id"], traced=traced),
                fingerprint(protocol),
                task,
                "baseline",
                0,
            )
            for traced in (True, False)
        ]
        row = {"task_id": task["id"], "availability": [item["availability"] for item in pair]}
        if all(item["receipt"] is not None for item in pair):
            traced, untraced = [item["receipt"] for item in pair]
            row.update(
                traced_ms=traced["elapsed_ms"],
                untraced_ms=untraced["elapsed_ms"],
                difference_ms=traced["elapsed_ms"] - untraced["elapsed_ms"],
                direct_recording_ms=traced["recording_ms"],
                matching_final_answer=traced["output"]["answer"] == untraced["output"]["answer"],
                matching_model_outputs=[item["output_text"] for item in traced["model_calls"]]
                == [item["output_text"] for item in untraced["model_calls"]],
                traced_status=traced["status"],
                untraced_status=untraced["status"],
            )
        rows.append(row)
    return {
        "protocol_hash": fingerprint(protocol),
        "planned_pairs": len(rows),
        "recorded_pairs": sum("difference_ms" in row for row in rows),
        "rows": rows,
        "direct_recording_ms": summarize_values([row.get("direct_recording_ms") for row in rows]),
        "wall_clock_difference_ms": summarize_values([row.get("difference_ms") for row in rows]),
        "limits": "Six pilot pairs, fixed order, shared host. Wall-clock differences include inference and warm-state variation and are not a causal overhead estimate. Direct recording time covers span construction/input serialization, excluding final export, analysis, and context setup.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = summarize(args.protocol, args.root)
    write_new(args.out, result)
    print(json.dumps({key: value for key, value in result.items() if key != "rows"}))
