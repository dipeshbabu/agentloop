"""Export synthetic harness decisions through native traces and offline HTML."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agentloop import trace_agent, trace_tool
from agentloop.findings import build_diagnosis
from agentloop.harness import Decision, Harness, HarnessConfig, HarnessDeniedError, Hook, Policy
from agentloop.html_report import analysis_to_html
from agentloop.optimizer import build_optimization_plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/harness-evidence"))
    out = parser.parse_args().out
    out.mkdir(parents=True, exist_ok=True)
    policy = Policy(
        "reviewed-tool",
        "1",
        lambda context: Decision("deny", "requires_review"),
        hooks={Hook("tool")},
        actions={"deny"},
    )
    for mode in ("disabled", "shadow", "enforce"):

        @trace_tool()
        def tool() -> str:
            return "synthetic result"

        with trace_agent(f"harness-{mode}", metadata={"synthetic": True}) as trace:
            run = Harness(HarnessConfig(mode, (policy,))).start_run()
            try:
                run.wrap(tool, boundary="tool")()
            except HarnessDeniedError:
                pass
        trace.export_json(out / f"{mode}.json")
        evidence = run.export_evidence()
        (out / f"{mode}-decisions.json").write_text(
            json.dumps(evidence, indent=2) + "\n", encoding="utf-8"
        )
        payload = {
            "trace": trace.to_dict(),
            "report": trace.report(),
            "diagnosis": build_diagnosis(trace),
            "optimization": build_optimization_plan(trace),
        }
        (out / f"{mode}.html").write_text(analysis_to_html(payload), encoding="utf-8")
        print(
            f"{mode}: {len(evidence['decisions'])} decisions; {trace.report()['tool_call_count']} tool calls"
        )
    print("Synthetic control-flow evidence; no measured policy-benefit claim.")


if __name__ == "__main__":
    main()
