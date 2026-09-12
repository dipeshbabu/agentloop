"""Write deterministic synthetic study traces; no external model calls."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentloop.events import AgentEvent
from agentloop.tracer import AgentTrace


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/study-demo"))
    args = parser.parse_args()
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for condition in ("baseline", "compressed"):
        for task in range(3):
            for seed in range(2):
                duration = 1000 + task * 100 + seed * 20
                if condition == "compressed":
                    duration -= 150
                failed = condition == "compressed" and task == 2 and seed == 1
                run_id = f"{condition}-task{task}-seed{seed}"
                end = start + timedelta(milliseconds=duration)
                trace = AgentTrace(
                    name=condition,
                    run_id=run_id,
                    started_at=start.isoformat(),
                    ended_at=end.isoformat(),
                    elapsed_ms=duration,
                    metadata={
                        "task_id": task,
                        "seed": seed,
                        "success": not failed,
                        "quality_score": 0.0 if failed else 1.0,
                        "synthetic": True,
                    },
                )
                trace.add_event(
                    AgentEvent(
                        event_id=f"{run_id}-model",
                        run_id=run_id,
                        event_type="model_call",
                        name="answer",
                        started_at=start.isoformat(),
                        ended_at=end.isoformat(),
                        duration_ms=duration,
                        model="unpriced-example" if failed else "gpt-4o-mini",
                        input_tokens=500,
                        output_tokens=100,
                        token_provenance="user_supplied",
                        status="error" if failed else "ok",
                        metadata={"error_type": "timeout"} if failed else {},
                    )
                )
                trace.export_json(args.out / condition / f"{run_id}.json")
    manifest = {
        "schema_version": "1.0",
        "name": "Synthetic compression study",
        "baseline": "baseline",
        "conditions": {"baseline": ["baseline/*.json"], "compressed": ["compressed/*.json"]},
        "pairing_keys": ["task_id", "seed"],
        "bootstrap": {"samples": 1000, "seed": 7, "confidence": 0.95},
    }
    (args.out / "study.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote synthetic study manifest to {args.out / 'study.json'}")


if __name__ == "__main__":
    main()
