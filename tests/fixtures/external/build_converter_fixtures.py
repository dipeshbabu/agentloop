"""Regenerate owned synthetic outputs with explicitly selected upstream source.

This optional developer command requires Python 3.12+ and opentelemetry-proto
1.42.1. Ordinary importer tests only read the frozen JSON; they never import the
upstream converter or execute this command.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import sys
from hashlib import sha256
from pathlib import Path

REVISION = "d5ac1be17f575852eaf4fffc4072fd18481c209b"
ROOT = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--converter-source", type=Path, required=True)
    parser.add_argument("--source-provenance", type=Path, required=True)
    args = parser.parse_args()
    provenance = json.loads(args.source_provenance.read_text(encoding="utf-8"))
    if provenance["revision"] != REVISION:
        raise ValueError("converter source revision differs from the frozen contract")
    for name in ("convert.py", "ids.py", "_types.py"):
        relative = f"packages/harbor-atif2otel/src/harbor_atif2otel/{name}"
        if (
            sha256((args.converter_source / "harbor_atif2otel" / name).read_bytes()).hexdigest()
            != provenance["files_sha256"][relative]
        ):
            raise ValueError("selected converter source bytes differ from provenance")
    sys.path.insert(0, str(args.converter_source.resolve()))
    from harbor_atif2otel.convert import convert_trajectory, resource_spans_to_otlp_json

    cases = []
    for name in (
        "atif_v17_simple",
        "atif_v18_multimodal",
        "atif_aggregated_llm_calls",
        "atif_deterministic_dispatch",
        "atif_embedded_subagents",
    ):
        source = ROOT / "harbor" / f"{name}.json"
        payload = json.loads(source.read_text(encoding="utf-8"))
        converted = resource_spans_to_otlp_json(
            convert_trajectory(payload, trace_seed=f"agentloop_owned_{name}")
        )
        target = ROOT / "otlp" / f"converter_{name}.jsonl"
        data = (
            json.dumps(converted, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode("utf-8")
        target.write_bytes(data)
        cases.append(
            {
                "input": f"harbor/{name}.json",
                "input_sha256": sha256(source.read_bytes()).hexdigest(),
                "output": f"otlp/{target.name}",
                "output_sha256": sha256(data).hexdigest(),
                "schema_version": payload["schema_version"],
                "trace_seed": f"agentloop_owned_{name}",
            }
        )
    (ROOT / "otlp/converter_provenance.json").write_bytes(
        (
            json.dumps(
                {
                    "schema_version": "1.0",
                    "fixture_origin": "owned_synthetic_input_through_pinned_upstream_converter",
                    "producer": "harbor-atif2otel",
                    "upstream_project_version": "0.1.1",
                    "emitted_sdk_version": "0.1.0",
                    "upstream_revision": REVISION,
                    "upstream_license": "Apache-2.0",
                    "source_files_sha256": provenance["files_sha256"],
                    "python": sys.version.split()[0],
                    "opentelemetry_proto": importlib.metadata.version("opentelemetry-proto"),
                    "protobuf": importlib.metadata.version("protobuf"),
                    "live_execution": False,
                    "media_files_read": False,
                    "cases": cases,
                },
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
    )
    print(f"Frozen {len(cases)} actual converter outputs from owned inputs")


if __name__ == "__main__":
    main()
