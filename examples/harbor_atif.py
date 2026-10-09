"""Import owned synthetic ATIF and render ordinary native analysis offline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agentloop.entrypoint import _analysis_payload
from agentloop.html_report import analysis_to_html
from agentloop.integrations.harbor.atif import AtifOptions, import_atif


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/harbor-atif"))
    args = parser.parse_args()
    fixture = (
        Path(__file__).resolve().parents[1]
        / "tests/fixtures/external/harbor/atif_embedded_subagents.json"
    )
    result = import_atif(fixture, options=AtifOptions(synthetic_fixture=True))
    result.write(args.out)
    for trace in result.traces:
        analysis = _analysis_payload(trace)
        (args.out / f"{trace.run_id}.analysis.json").write_text(
            json.dumps(analysis, indent=2) + "\n", encoding="utf-8"
        )
        (args.out / f"{trace.run_id}.html").write_text(analysis_to_html(analysis), encoding="utf-8")
    print(
        f"Imported {len(result.traces)} synthetic source trajectories; reported usage is retained, latency and task correctness remain unavailable."
    )
    print(f"Native traces, receipts and HTML: {args.out}")


if __name__ == "__main__":
    main()
