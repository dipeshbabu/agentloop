"""Offline synthetic Harbor inventory and native study reproduction."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agentloop.entrypoint import _analysis_payload
from agentloop.html_report import analysis_to_html
from agentloop.integrations.harbor.manifest import write_harbor_study
from agentloop.integrations.harbor.trials import HarborOptions, import_harbor
from agentloop.integrations.harbor.verifier import ScoringContract
from agentloop.studies import summarize_study


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/harbor-trials"))
    args = parser.parse_args()
    fixture = Path(__file__).resolve().parents[1] / "tests/fixtures/external/harbor/mixed_job"
    scoring = ScoringContract.all_gte("synthetic-correctness-v1", {"correctness": 1.0})
    context = {item.name: {"repetition": item.name} for item in fixture.iterdir() if item.is_dir()}
    conditions = {
        name: import_harbor(
            fixture,
            options=HarborOptions(
                job_id=f"synthetic-{name}",
                condition=name,
                protocol_id="synthetic-protocol-v1",
                trial_context=context,
                scoring=scoring,
                synthetic_fixture=True,
            ),
        )
        for name in ("baseline", "candidate")
    }
    path = write_harbor_study(
        conditions, args.out, baseline="baseline", name="Synthetic Harbor inventory study"
    )
    report = summarize_study(path)
    (args.out / "study-report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    trace = next(trace for trace in conditions["baseline"].trial_traces if trace.name == "pass")
    (args.out / "trial-report.html").write_text(
        analysis_to_html(_analysis_payload(trace)), encoding="utf-8"
    )
    print(
        f"Synthetic source study: {path}; complete population: {args.out / 'cohort-inventory.json'}"
    )
    print(
        "Both conditions use the same owned fixtures. No measured improvement or live execution is claimed."
    )


if __name__ == "__main__":
    main()
