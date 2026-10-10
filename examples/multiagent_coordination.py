"""Reproduce owned coordination evidence, missing timing and noncausal links."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agentloop.integrations.harbor.atif import AtifOptions, import_atif
from agentloop.integrations.omnigent.telemetry import import_omnigent
from agentloop.interoperability.coordination import summarize_coordination
from agentloop.interoperability.otlp_jsonl import OtlpOptions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/coordination-example"))
    args = parser.parse_args()
    fixtures = Path(__file__).resolve().parents[1] / "tests/fixtures/external"
    args.out.mkdir(parents=True, exist_ok=True)
    results = {
        "harbor-shared-session": import_atif(
            fixtures / "harbor/atif_embedded_subagents.json",
            options=AtifOptions(synthetic_fixture=True),
        ),
        "omnigent-noncausal-link": import_omnigent(
            fixtures / "omnigent/parent_child_multi_trace.otlp.json",
            options=OtlpOptions(system="omnigent", synthetic_fixture=True),
        ),
    }
    spans = []
    for index, (name, role, start, end) in enumerate(
        (
            ("implementer", "implementer", 0, 10),
            ("reviewer", "reviewer", 1, 6),
            ("responder", "responder", 3, 8),
        ),
        start=1,
    ):
        span = {
            "traceId": "a" * 32,
            "spanId": str(index) * 16,
            "name": "agent",
            "startTimeUnixNano": str(1767225600000000000 + start * 1_000_000_000),
            "endTimeUnixNano": str(1767225600000000000 + end * 1_000_000_000),
            "status": {"code": "STATUS_CODE_ERROR" if index == 3 else "STATUS_CODE_OK"},
            "attributes": [
                {"key": key, "value": {"stringValue": value}}
                for key, value in {
                    "openinference.span.kind": "AGENT",
                    "session.id": "owned-polly-like",
                    "gen_ai.agent.name": name,
                    "agent.role": role,
                }.items()
            ],
        }
        if index != 1:
            span["parentSpanId"] = "1" * 16
        spans.append(span)
    source = args.out / "owned-reviewers.json"
    content = (
        json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}, indent=2) + "\n"
    ).encode()
    if source.exists() and source.read_bytes() != content:
        raise ValueError("owned input already exists with conflicting bytes")
    source.write_bytes(content)
    results["omnigent-owned-reviewers"] = import_omnigent(
        source, options=OtlpOptions(system="omnigent", synthetic_fixture=True)
    )
    for name, result in results.items():
        result.write(args.out / name)
        report = summarize_coordination(result.traces, result.source_receipts)
        print(
            f"{name}: {len(report['actors'])} actors, {report['observed_child_count']} observed children, {report['unknown_child_count']} unknown children; overlap {report['parallel_overlap_ms']} ms; caller wait {report['handoff_wait_ms']}"
        )
    print(
        "Owned synthetic inputs. Child timing is not caller wait; reviewer labels establish no quality benefit. JSON/HTML reports are local."
    )


if __name__ == "__main__":
    main()
