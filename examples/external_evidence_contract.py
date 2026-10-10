"""Inspect synthetic receipts offline; this example does not import a job."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agentloop.interoperability.contracts import ImportReceipt, summarize_receipts
from agentloop.interoperability.validation import load_json_artifact


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/external-contract"))
    args = parser.parse_args()
    fixtures = Path(__file__).resolve().parents[1] / "tests/fixtures/external"
    artifact = load_json_artifact(fixtures, "expected/imported_receipts.json")
    receipts = [ImportReceipt.from_dict(payload) for payload in artifact.payload]
    args.out.mkdir(parents=True, exist_ok=True)
    report = summarize_receipts(receipts)
    report["synthetic"] = True
    (args.out / "inventory.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        f"{report['receipts']} synthetic receipts; {report['native_traces']} measured native traces; {report['quality_indeterminate']} indeterminate quality outcomes"
    )
    print(f"Inventory: {args.out / 'inventory.json'}")


if __name__ == "__main__":
    main()
