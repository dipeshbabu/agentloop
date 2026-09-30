"""Pin training, task groups, learned parameters and owner-delegated quality criteria."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np

from examples.non_agent_study.data import SOURCES, digest, read, write_new
from examples.non_agent_study.models import train

ROOT = Path(__file__).resolve().parents[2]


def _tasks(rows, *, name, split, batch_size, count):
    selected = []
    seen = set()
    if name == "record-linkage":
        positives = [row for row in rows if row["label"] == "TRUE"]
        negatives = [row for row in rows if row["label"] == "FALSE"]
        rows = [row for pair in zip(positives, negatives) for row in pair]
    for row in rows:
        groups = set(row.get("groups", [row.get("group", row["id"])]))
        if seen.intersection(groups):
            continue
        seen.update(groups)
        selected.append(row)
        if len(selected) == batch_size * count:
            break
    if len(selected) != batch_size * count:
        raise ValueError("insufficient disjoint task rows")
    return [
        {
            "id": f"{name}-{split}-{index:02d}",
            "split": split,
            "rows": selected[index * batch_size : (index + 1) * batch_size],
        }
        for index in range(count)
    ]


def freeze(prepared, out, *, source_revision):
    prepared, out = Path(prepared), Path(out)
    out.mkdir(parents=True, exist_ok=False)
    if len(source_revision) != 40 or any(
        char not in "0123456789abcdef" for char in source_revision
    ):
        raise ValueError("pin a full source revision")
    workloads = []
    for name in SOURCES:
        data = read(prepared / (name + ".json"))
        content = dict(data)
        if content.pop("sha256") != digest(content):
            raise ValueError("prepared dataset hash mismatch")
        training = data["folds"]["train"][:4096]
        definitions = []
        if name == "record-linkage":
            definitions = [("baseline", "logistic", None), ("cheap", "logistic", (0, 2))]
        elif name == "dry-bean":
            definitions = [("primary", "centroid", None), ("fallback", "knn", None)]
        else:
            definitions = [("baseline", "knn", None), ("cheap", "centroid", None)]
        models = {}
        for identity, algorithm, indices in definitions:
            parameters = train(
                training,
                algorithm=algorithm,
                feature_indices=indices,
                missing_indicators=name == "record-linkage",
            )
            path = out / "models" / (name + "-" + identity + ".json")
            write_new(path, parameters)
            models[identity] = {
                "path": path.relative_to(out).as_posix(),
                "sha256": parameters["sha256"],
                "algorithm": algorithm,
                "version": "1.0",
            }
        tasks = _tasks(
            data["folds"]["calibration"],
            name=name,
            split="fit",
            batch_size=16 if name == "record-linkage" else 8,
            count=8,
        )
        tasks += _tasks(
            data["folds"]["evaluation"],
            name=name,
            split="held_out",
            batch_size=16 if name == "record-linkage" else 8,
            count=32,
        )
        for index in range(0, len(training), 512):
            write_new(
                out / "training" / name / f"part-{index // 512:02d}.json",
                training[index : index + 512],
            )
        workloads.append(
            {
                "id": name,
                "version": "1.0",
                "category": {
                    "banknote": "image-feature classification",
                    "record-linkage": "batch record matching",
                    "dry-bean": "multi-stage confidence/fallback decision",
                }[name],
                "source": data["source"],
                "prepared_sha256": data["sha256"],
                "split_basis": data["split_basis"],
                "models": models,
                "training_count": len(training),
                "tasks": tasks,
                "repetitions": 2,
                "warmup": {
                    "repetitions": 2,
                    "input": {
                        key: value
                        for key, value in training[0].items()
                        if key in {"id", "features", "left_ref", "right_ref"}
                    },
                    "scope": "unscored training input; excluded from task timing",
                },
                "variants": ["batched", "cheap"],
                "fallback_margin": 0.35 if name == "dry-bean" else None,
                "quality": {
                    "scorer": "matches" if name == "record-linkage" else "decision",
                    "version": "1.0",
                    "minimum_score": 0.95
                    if name == "record-linkage"
                    else 0.90
                    if name == "banknote"
                    else 0.85,
                    "max_regression": 0,
                    "criterion": "Independent published dataset labels; candidate must meet the score floor and not lower paired baseline score.",
                },
                "selection": {"batched": "batch_model_calls", "cheap": "route_to_smaller_model"},
                "attribution": {
                    "batched": "combined_configuration_unattributed"
                    if name == "dry-bean"
                    else "isolated_batching",
                    "cheap": "combined_configuration_unattributed",
                },
            }
        )
    files = sorted(
        [*(ROOT / "agentloop").rglob("*.py"), *(ROOT / "examples/non_agent_study").glob("*.py")]
    )
    import hashlib

    protocol = {
        "schema_version": "1.0",
        "name": "Actual non-agent CPU workload validation",
        "version": "1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_revision": source_revision,
        "source_hashes": {
            path.relative_to(ROOT).as_posix(): hashlib.sha256(
                path.read_text(encoding="utf-8").encode()
            ).hexdigest()
            for path in files
        },
        "numpy_version": np.__version__,
        "workloads": workloads,
        "synthetic": False,
        "data_kind": "actual learned-model execution on public benchmark data; research applications, not customer production traffic",
        "owner_quality_approval": "Repository owner delegated workload/scorer choices in this session ('use as per yours'); criteria are frozen published-label accuracy/F1 and paired non-regression, not invented domain policy.",
        "pairing_keys": ["workload", "task_id", "repetition", "variant", "protocol_sha256"],
        "bootstrap": {"samples": 1000, "seed": 20260930, "confidence": 0.95},
        "execution_order": "Model fitting uses train only. Fit/held-out task groups fixed before execution. Baseline findings/selection saved before both candidates. All candidates and recording-disabled controls retained.",
        "accounting": {
            "paid_provider_spend_usd": 0,
            "operating_cost_usd": None,
            "token_usage": "not applicable to numeric CPU models; native provenance unavailable, never fabricated tokens",
        },
        "limits": "Small public-data research applications with fixed condition order and hardware. Learned confidence scores are heuristics, not calibrated probabilities. No operational authentication, identity linkage or agricultural decisions are deployed.",
    }
    protocol["sha256"] = digest(protocol)
    write_new(out / "protocol.json", protocol)
    return protocol


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    args = parser.parse_args()
    result = freeze(args.prepared, args.out_dir, source_revision=args.source_revision)
    print(result["sha256"])
