"""Offline Omnigent session/policy import and source-qualified local reports."""

from __future__ import annotations

import argparse
from pathlib import Path

from agentloop.entrypoint import _analysis_payload
from agentloop.html_report import analysis_to_html
from agentloop.integrations.omnigent.telemetry import import_omnigent
from agentloop.interoperability.otlp_jsonl import OtlpOptions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/omnigent-example"))
    args = parser.parse_args()
    fixture = Path(__file__).resolve().parents[1] / "tests/fixtures/external/omnigent"
    for name in (
        "agent_tool_policy",
        "parent_child_multi_trace",
        "unverified_policy_decision",
        "missing_parentage",
    ):
        result = import_omnigent(
            fixture / f"{name}.otlp.json",
            options=OtlpOptions(system="omnigent", synthetic_fixture=True),
        )
        folder = args.out / name
        result.write(folder)
        for index, trace in enumerate(result.traces):
            (folder / f"report-{index}.html").write_text(
                analysis_to_html(_analysis_payload(trace)), encoding="utf-8"
            )
        stats = result.inventory()
        print(
            f"{name}: {stats['native_traces']} traces, {stats['session_groups']} session groups, policy actions {stats['policy_actions']}"
        )
    print(
        "Owned synthetic source observations; no verified prevention, task pass or performance win is claimed."
    )


if __name__ == "__main__":
    main()
