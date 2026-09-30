"""Deterministic public-data preparation; labels and identities never enter features."""

from __future__ import annotations

import csv
import hashlib
import heapq
import io
import json
import zipfile
from collections import Counter
from pathlib import Path

SOURCES = {
    "banknote": {
        "uci_id": 267,
        "file": "banknote.zip",
        "sha256": "1e2acd9a2085fadf3d8145c12d3d22af853320d52294a6590c2eaf75fdc05227",
        "url": "https://archive.ics.uci.edu/static/public/267/banknote%2Bauthentication.zip",
        "doi": "10.24432/C55P57",
    },
    "record-linkage": {
        "uci_id": 210,
        "file": "record-linkage.zip",
        "sha256": "6dda273692319075ef7752ee7c9b694e65e956daea3ea05eb6613bf8bfc33e49",
        "url": "https://archive.ics.uci.edu/static/public/210/record%2Blinkage%2Bcomparison%2Bpatterns.zip",
        "doi": "10.24432/C51K6B",
    },
    "dry-bean": {
        "uci_id": 602,
        "file": "dry-bean.zip",
        "sha256": "0a64eff5be87f48c3dbbfc0a12a56c5d5b5167ef8e61cd45d69b3e7c7130c06f",
        "url": "https://archive.ics.uci.edu/static/public/602/dry%2Bbean%2Bdataset.zip",
        "doi": "10.24432/C50S4B",
    },
}
SEED = "agentloop-nonagent-2026-09-v1"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def source_path(root, name):
    path = Path(root) / SOURCES[name]["file"]
    if hashlib.sha256(path.read_bytes()).hexdigest() != SOURCES[name]["sha256"]:
        raise ValueError("public dataset archive checksum mismatch")
    return path


def fold(identity):
    value = int(digest([SEED, identity])[:8], 16) % 100
    return "train" if value < 60 else "calibration" if value < 80 else "evaluation"


def _numeric_rows(root, name):
    with zipfile.ZipFile(source_path(root, name)) as archive:
        if name == "banknote":
            lines = archive.read("data_banknote_authentication.txt").decode("utf-8").splitlines()
        else:
            content = archive.read("DryBeanDataset/Dry_Bean_Dataset.arff").decode("utf-8")
            lines = content[content.lower().index("@data") + 5 :].splitlines()
        for index, row in enumerate(
            csv.reader(line for line in lines if line.strip() and not line.startswith("%"))
        ):
            features = [float(value) + 0.0 for value in row[:-1]]
            if len(features) != (4 if name == "banknote" else 16):
                raise ValueError("unexpected public feature dimensions")
            yield {
                "id": f"{name}-row-{index:05d}",
                "features": features,
                "label": row[-1].strip(),
                "group": digest(features),
                "source_row": index,
            }


def prepare_numeric(root, name):
    folds = {key: [] for key in ("train", "calibration", "evaluation")}
    seen = {}
    duplicates = conflicts = 0
    for row in _numeric_rows(root, name):
        key = row["group"]
        if key in seen:
            duplicates += 1
            conflicts += seen[key] != row["label"]
        else:
            seen[key] = row["label"]
        # Keep all source rows, but identical feature vectors share a split.
        folds[fold(key)].append(row)
    for rows in folds.values():
        rows.sort(key=lambda row: digest([SEED, row["id"]]))
    return {
        "schema_version": "1.0",
        "dataset": name,
        "source": SOURCES[name],
        "license": "CC-BY-4.0",
        "seed": SEED,
        "split_basis": "feature-vector groups, train60/calibration20/evaluation20 hash partitions",
        "duplicate_feature_rows": duplicates,
        "conflicting_label_duplicates": conflicts,
        "folds": folds,
        "counts": {key: len(rows) for key, rows in folds.items()},
    }


class Components:
    def __init__(self):
        self.parent = {}

    def find(self, value):
        parent = self.parent
        while value in parent and parent[value] != value:
            parent[value] = parent.get(parent[value], parent[value])
            value = parent[value]
        return value

    def union(self, left, right):
        left, right = self.find(left), self.find(right)
        if left != right:
            small, large = sorted((left, right))
            self.parent[large] = small


def _linkage_rows(path):
    with zipfile.ZipFile(path) as outer:
        with zipfile.ZipFile(io.BytesIO(outer.read("donation.zip"))) as donation:
            for block in range(1, 11):
                with zipfile.ZipFile(io.BytesIO(donation.read(f"block_{block}.zip"))) as archive:
                    names = [name for name in archive.namelist() if name.endswith(".csv")]
                    if len(names) != 1:
                        raise ValueError("unexpected linkage block contents")
                    with archive.open(names[0]) as raw:
                        reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8"))
                        header = next(reader)
                        if (
                            header[:2] != ["id_1", "id_2"]
                            or header[-1] != "is_match"
                            or len(header) != 12
                        ):
                            raise ValueError("unexpected linkage schema")
                        for index, row in enumerate(reader):
                            yield block, index, row


def prepare_linkage(root):
    path = source_path(root, "record-linkage")
    components = Components()
    total_rows = positive_rows = 0
    for _, _, row in _linkage_rows(path):
        total_rows += 1
        if row[-1] == "TRUE":
            components.union(int(row[0]), int(row[1]))
            positive_rows += 1
    print("Linkage component pass:", total_rows, positive_rows, flush=True)
    heaps = {
        (split, label): []
        for split in ("train", "calibration", "evaluation")
        for label in ("FALSE", "TRUE")
    }
    limits = {
        "train": {"FALSE": 3072, "TRUE": 1024},
        "calibration": {"FALSE": 1024, "TRUE": 1024},
        "evaluation": {"FALSE": 2048, "TRUE": 1024},
    }
    assignment = {}
    eligible = Counter()
    for block, index, row in _linkage_rows(path):
        left, right = components.find(int(row[0])), components.find(int(row[1]))
        for group in (left, right):
            if group not in assignment:
                assignment[group] = fold(["linkage-component", group])
        split = assignment[left]
        if split != assignment[right]:
            continue
        label = row[-1]
        eligible[split + ":" + label] += 1
        priority = int(digest([SEED, "pair", row[0], row[1]]), 16)
        heap = heaps[split, label]
        if len(heap) == limits[split][label] and priority >= -heap[0][0]:
            continue
        features = [None if value == "?" else float(value) for value in row[2:-1]]
        record = {
            "id": f"linkage-{block:02d}-{index:06d}",
            "features": features,
            "label": label,
            "groups": sorted({digest(["component", left]), digest(["component", right])}),
            "left_ref": digest(["record", row[0]]),
            "right_ref": digest(["record", row[1]]),
            "source_block": block,
            "source_row": index,
        }
        item = (-priority, block, index, record)
        if len(heap) < limits[split][label]:
            heapq.heappush(heap, item)
        else:
            heapq.heapreplace(heap, item)
    folds = {}
    for split in ("train", "calibration", "evaluation"):
        rows = []
        used = set()
        for label in ("TRUE", "FALSE"):
            desired = (
                1024 if split == "train" and label == "TRUE" else 3072 if split == "train" else 256
            )
            chosen = 0
            for _, _, _, row in sorted(heaps[split, label], reverse=True):
                # Held-out/calibration rows are component-disjoint within each fold too.
                if split != "train" and used.intersection(row["groups"]):
                    continue
                rows.append(row)
                used.update(row["groups"])
                chosen += 1
                if chosen == desired:
                    break
            if chosen < desired:
                raise ValueError(f"insufficient disjoint {split} {label} rows: {chosen}/{desired}")
        rows.sort(key=lambda row: digest([SEED, row["id"]]))
        folds[split] = rows
    return {
        "schema_version": "1.0",
        "dataset": "record-linkage",
        "source": SOURCES["record-linkage"],
        "license": "CC-BY-4.0",
        "seed": SEED,
        "total_source_rows": total_rows,
        "positive_source_rows": positive_rows,
        "eligible_counts": dict(eligible),
        "split_basis": "positive-match connected components; retain pairs only when both endpoints share a hash partition",
        "redaction": "Raw record identifiers omitted; hashes are references, not a claim of anonymity. Identifiers never enter model features.",
        "sampling": "train1024matches3072nonmatches; calibration/evaluation256ofeach with no shared record components within fold; not production prevalence",
        "folds": folds,
        "counts": {key: len(rows) for key, rows in folds.items()},
    }


def prepare(sources, out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    for name in SOURCES:
        value = (
            prepare_linkage(sources) if name == "record-linkage" else prepare_numeric(sources, name)
        )
        value["sha256"] = digest(value)
        write_new(out / (name + ".json"), value)
        print(name, value["counts"], value["sha256"], flush=True)
    return out


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.sources, args.out_dir)
