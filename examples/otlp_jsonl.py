"""Offline JSONL import, deduplication and qualified local HTML."""

from __future__ import annotations

import argparse
from pathlib import Path

from agentloop.entrypoint import _analysis_payload
from agentloop.html_report import analysis_to_html
from agentloop.interoperability.otlp_jsonl import OtlpOptions, import_otlp


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/otlp-example"))
    args = parser.parse_args()
    fixtures = Path(__file__).resolve().parents[1] / "tests/fixtures/external/otlp"
    for folder, filename in (
        ("segments", "duplicate_trace_ids.jsonl"),
        ("converter", "converter_atif_v18_multimodal.jsonl"),
        ("partial", "malformed_mixed_valid.jsonl"),
    ):
        result = import_otlp(
            fixtures / filename, options=OtlpOptions(system="harbor", synthetic_fixture=True)
        )
        result.write(args.out / folder)
        for index, trace in enumerate(result.traces):
            (args.out / folder / f"report-{index}.html").write_text(
                analysis_to_html(_analysis_payload(trace)), encoding="utf-8"
            )
        stats = result.inventory()
        print(
            f"{folder}: {stats['raw_spans']} source spans, {stats['native_traces']} traces, {stats['identical_duplicate_spans']} duplicates, {stats['invalid_records']} invalid records"
        )
    print(
        "Owned synthetic inputs. Converter timing remains inferred; no live execution or optimization win is claimed."
    )


if __name__ == "__main__":
    main()
