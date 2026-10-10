"""Reproduce synthetic correct/incorrect full-population cohort comparisons."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from agentloop.integrations.harbor.trials import HarborOptions, import_harbor
from agentloop.integrations.harbor.verifier import ScoringContract
from agentloop.interoperability.cohorts import CohortProtocol, write_cohort_study


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/harbor-cross-harness"))
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / "tests/fixtures/external/harbor/mixed_job/pass"
    protocol = CohortProtocol("owned-cross-harness-v1")
    scoring = ScoringContract.all_gte("owned-correctness-v1", {"correctness": 1})
    conditions = {}
    for condition in ("baseline", "correct-candidate", "incorrect-candidate"):
        job = args.out / "inputs" / condition
        if not job.exists():
            for task, digest in (("blue", "a" * 64), ("green", "b" * 64)):
                for repetition in (1, 2):
                    name = f"{condition}-{task}-{repetition}"
                    trial = job / name
                    shutil.copytree(source, trial)
                    result = json.loads((trial / "result.json").read_text(encoding="utf-8"))
                    seconds = 4 if condition == "baseline" else 2
                    quality = 0 if condition == "incorrect-candidate" else 1
                    result.update(
                        id=name,
                        trial_name=name,
                        task_name=task,
                        task_id={"path": f"owned-tasks/{task}"},
                        task_checksum=digest,
                        repetition=repetition,
                        protocol_id=protocol.protocol_id,
                        dataset_version="owned-v1",
                    )
                    result["agent_execution"]["finished_at"] = f"2026-01-01T00:00:{seconds:02d}Z"
                    result["agent_result"] = {
                        "n_input_tokens": 12,
                        "n_output_tokens": 6,
                        "n_cache_tokens": 4,
                        "cost_usd": 0.1,
                    }
                    result["verifier_result"] = {"rewards": {"correctness": quality}}
                    lock = json.loads((trial / "lock.json").read_text(encoding="utf-8"))
                    lock["task"]["digest"] = "sha256:" + digest
                    for filename, value in (
                        ("result.json", result),
                        ("lock.json", lock),
                        ("verifier/reward.json", {"correctness": quality}),
                    ):
                        (trial / filename).write_bytes(
                            (json.dumps(value, indent=2) + "\n").encode()
                        )
            (job / "result.json").write_bytes(
                (json.dumps({"id": f"owned-{condition}", "n_total_trials": 5}) + "\n").encode()
            )
        conditions[condition] = import_harbor(
            job,
            options=HarborOptions(
                scoring=scoring,
                condition=condition,
                protocol_id=protocol.protocol_id,
                synthetic_fixture=True,
            ),
        )
    result = write_cohort_study(
        conditions,
        args.out / "report",
        baseline="baseline",
        protocol=protocol,
        name="Owned synthetic full-population comparison",
    )
    for condition, comparison in result.report()["comparisons"].items():
        rate = comparison["quality_preserving_intervention_rate"]
        print(
            f"{condition}: {rate['numerator']}/{rate['denominator']} verified improvements; {comparison['state_counts']}"
        )
    print(f"Native manifest: {result.manifest_path}; JSON/HTML: {args.out / 'report'}")
    print(
        "Synthetic validation only. Both candidates are faster; the incorrect one is rejected. No causal or empirical benefit is claimed."
    )


if __name__ == "__main__":
    main()
