from __future__ import annotations

from pathlib import Path

from agentloop.demo import run_baseline, run_optimized


def main() -> None:
    out_dir = Path("runs/ci_pr_demo")
    baseline = run_baseline(out_dir)
    candidate = run_optimized(out_dir)
    print("Generated AgentLoop CI trace artifacts:")
    print(f"  baseline:  {baseline}")
    print(f"  candidate: {candidate}")
    print()
    print("Run the same gate used by the PR workflow:")
    print(
        "agentloop ci "
        f"--baseline {baseline} "
        f"--candidate {candidate} "
        "--out runs/ci_pr_demo/agentloop_ci.md "
        "--json-out runs/ci_pr_demo/agentloop_ci.json "
        "--min-latency-improvement-pct 20 "
        "--min-cost-improvement-pct 5"
    )


if __name__ == "__main__":
    main()
