"""Exercise an installed wheel's evidence workflow outside the source checkout.

Run with the wheel environment's Python: python -I scripts/smoke_installed_wheel.py
--expected-version X.Y.Z. Only the synthetic example is read from the checkout.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # nosec B404 - fixed Python argv, no shell
import sys
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

import agentloop
from agentloop.version import __version__


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def smoke(expected_version: str) -> None:
    require(sys.flags.isolated == 1, "Run this check with python -I")
    require(
        Path(agentloop.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()),
        "AgentLoop must be installed in this environment, not imported from the checkout",
    )
    require(
        version("agentloop-profiler") == __version__ == expected_version,
        "Installed distribution and import versions must match the release",
    )
    example = Path(__file__).resolve().parents[1] / "examples" / "intervention_study.py"
    with TemporaryDirectory(prefix="agentloop-wheel-smoke-") as temporary:
        work = Path(temporary)
        env = {
            **os.environ,
            "AGENTLOOP_STORE_BACKEND": "sqlite",
            "AGENTLOOP_SQLITE_PATH": str(work / "evidence" / "ledger.db"),
        }

        def run(*args: str) -> None:
            subprocess.run(  # nosec B603 - fixed interpreter and synthetic local arguments
                [sys.executable, "-I", *args], cwd=work, env=env, check=True
            )

        def cli(*args: str) -> None:
            run("-c", "from agentloop.entrypoint import app; app()", *args)

        cli("quickstart", "--out", "quickstart.json")
        cli("analyze", "quickstart.json", "--html", "analysis.html", "--json-out", "analysis.json")
        analysis = read_json(work / "analysis.json")
        require(bool(analysis["diagnosis"]["findings"]), "Quickstart must produce findings")
        require((work / "analysis.html").stat().st_size > 0, "HTML report must be nonempty")

        run(str(example), "--out", "evidence")
        cli(
            "study",
            "summarize",
            "evidence/study.json",
            "--out",
            "study.md",
            "--json-out",
            "study.json",
        )
        study = read_json(work / "study.json")
        comparison = study["comparisons"]["candidate"]
        require(comparison["pair_count"] == 6, "All six synthetic pairs must be retained")
        require(comparison["metrics"]["cost_usd"]["missing_count"] == 1, "Unknown cost was lost")

        outcomes = read_json(work / "evidence" / "outcomes.json")
        require(outcomes["configured_gates_passed_count"] == 5, "Quality failure was lost")
        for request in sorted((work / "evidence" / "requests").glob("*.json")):
            cli("intervention-create", str(request), "--out", "created.json")
            created = read_json(work / "created.json")
            cli("intervention-get", created["intervention_id"], "--out", "retrieved.json")
            original = read_json(
                work / "evidence" / "interventions" / f"{created['intervention_id']}.json"
            )
            require(created == original == read_json(work / "retrieved.json"), "Ledger changed")
        records = [read_json(path) for path in (work / "evidence" / "interventions").glob("*.json")]
        rejected = [record for record in records if not record["gates_passed"]]
        require(len(rejected) == 1, "Expected one retained rejected intervention")
        require(rejected[0]["measured"]["quality"]["passed"] is False, "Quality failure missing")
        require(rejected[0]["measured"]["deltas"]["cost_usd_delta"] is None, "Unknown cost missing")
    print(f"Installed wheel {expected_version}: quickstart, HTML, study, and ledger smoke passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-version", required=True)
    smoke(parser.parse_args().expected_version)
