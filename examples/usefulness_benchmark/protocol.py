"""Freeze workloads, independent label policies and analyzer sources before runs."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from agentloop.version import __version__
from examples import (
    batch_data_pipeline,
    email_workflow,
    incident_triage,
    marketplace_workflow,
    payment_review,
)

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = {
    "email": (email_workflow, "run_email", "inspect_email", "development"),
    "marketplace": (marketplace_workflow, "run_listing", "inspect_listing", "evaluation"),
    "payment": (payment_review, "run_payment", None, "development"),
    "incident": (incident_triage, "run_incident", "inspect_incident", "evaluation"),
    "batch": (batch_data_pipeline, "run_chunk", None, "evaluation"),
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_hash(path):
    return hashlib.sha256(Path(path).read_text(encoding="utf-8").encode()).hexdigest()


def write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")


def read(path):
    from agentloop.aggregates import read_json

    return read_json(path)


def fixtures(name):
    if name != "batch":
        return json.loads(canonical(REGISTRY[name][0].FIXTURES))
    rows = batch_data_pipeline.dataset(32)
    return [
        {
            "id": f"chunk-{index:02d}",
            "input": rows[index * 4 : (index + 1) * 4],
            "expected": {row["id"]: row["expected"] for row in rows[index * 4 : (index + 1) * 4]},
        }
        for index in range(8)
    ]


def labels_for(name, fixture):
    """Labels derive from fixture obligations, never emitted findings or candidate outcomes."""
    labels = [
        {
            "rule_id": "batch_model_calls",
            "label": "no_opportunity" if name != "batch" else "ambiguous",
            "spans": [],
            "rationale": "The decision stages consume prior stage outputs; serial decisions cannot be batched together."
            if name != "batch"
            else "Independent records permit some batching, but one cross-stage batch is not justified; exact generic detector scope remains ambiguous.",
        },
        {
            "rule_id": "route_to_smaller_model",
            "label": "ambiguous",
            "spans": [],
            "rationale": "A model/rule substitution is testable, but correctness and whole-configuration effects cannot be assumed from span duration.",
        },
    ]
    if name in {"email", "marketplace", "incident"}:
        stage = {
            "email": "priority_repeat",
            "marketplace": "identity_recheck",
            "incident": "severity_repeat",
        }[name]
        retained = fixture["input"]["verification"]
        labels.append(
            {
                "rule_id": "semantic_redundancy",
                "label": "no_opportunity" if retained else "opportunity",
                "spans": [] if retained else [stage],
                "rationale": "The fixture explicitly requires independent verification."
                if retained
                else "The repeated label computation has no independent verification obligation; the fixture backend computes the same decision.",
            }
        )
    if name in {"email", "incident"}:
        labels.append(
            {
                "rule_id": "context_relevance",
                "label": "opportunity",
                "spans": ["category" if name == "email" else "severity"],
                "rationale": "The declared fixture context is unrelated to the supplied decision inputs and is never needed by the gold rule.",
            }
        )
    return labels


def freeze(path, *, source_revision, repetitions=2):
    if (
        not isinstance(source_revision, str)
        or len(source_revision) != 40
        or any(char not in "0123456789abcdef" for char in source_revision)
    ):
        raise ValueError("pin a full analyzer source revision")
    if type(repetitions) is not int or not 1 <= repetitions <= 5:
        raise ValueError("repetitions must be 1..5")
    source_files = sorted(
        [
            *(ROOT / "agentloop").rglob("*.py"),
            *(ROOT / "examples/real_agent_study").glob("*.py"),
            *ROOT.glob("examples/usefulness_benchmark/*.py"),
            ROOT / "examples/reference_support.py",
            *(Path(module.__file__) for module, *_ in REGISTRY.values()),
        ]
    )
    workloads = []
    for name, (module, _, _, split) in REGISTRY.items():
        cases = fixtures(name)
        workloads.append(
            {
                "id": name,
                "kind": "synthetic_reference",
                "version": "1.0",
                "split": split,
                "cases": [
                    {**fixture, "finding_labels": labels_for(name, fixture)} for fixture in cases
                ],
                "variants": list(module.VARIANTS[1:]),
                "repetitions": repetitions,
                "scorer": {
                    "version": "1.0",
                    "type": "structured_per_field" if name == "batch" else "fields",
                    "min_score": 1,
                },
                "selection_families": [
                    "semantic_redundancy",
                    "context_relevance",
                    "route_to_smaller_model",
                    "batch_model_calls",
                ],
                "attribution": "combined_configuration_unattributed",
            }
        )
    value = {
        "schema_version": "1.0",
        "name": "AgentLoop cross-workload usefulness",
        "version": "1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "analyzer": {
            "package_version": __version__,
            "source_revision": source_revision,
            "source_hash_encoding": "UTF-8 with universal newlines normalized to LF",
            "sources": {
                path.relative_to(ROOT).as_posix(): source_hash(path) for path in source_files
            },
        },
        "workloads": workloads,
        "historical": {
            "mode": "retrospective_reanalysis_without_new_inference",
            "workloads": ["repository", "sql", "math"],
            "real_agent_index": {
                "path": "research/real-agent-2026-09/index.json",
                "sha256": file_hash(ROOT / "research/real-agent-2026-09/index.json"),
            },
            "calibration_index": {
                "path": "research/empirical-calibration-2026-09/index.json",
                "sha256": file_hash(ROOT / "research/empirical-calibration-2026-09/index.json"),
            },
            "phases": ["retired_onboarding", "pilot", "evaluation"],
            "labels": [
                {
                    "rule_id": "batch_model_calls",
                    "label": "no_opportunity",
                    "spans": [],
                    "rationale": "Archived agents consume previous tool outputs between model decisions; batching sequential decisions is invalid.",
                },
                {
                    "rule_id": "route_to_smaller_model",
                    "label": "ambiguous",
                    "spans": [],
                    "rationale": "A routing investigation is not proof of a quality-preserving optimization; historical results are reported independently.",
                },
            ],
        },
        "pairing_keys": ["workload", "task_id", "repetition", "variant", "protocol_sha256"],
        "finding_label_sampling": "First repetition only per distinct task; all repetitions remain in emission, quality and resource inventories.",
        "split_policy": "Whole reference workloads are assigned once to development/evaluation; no fitting or tuning is done. These public maintained fixtures and retrospective archives are not blind unseen data.",
        "execution_order": "Freeze protocol; for each reference case/repetition save baseline and original diagnosis before any candidate; all named variants including negative controls run. Existing historical observations/predictions remain untouched.",
        "bootstrap": {"samples": 1000, "seed": 20260930, "confidence": 0.95},
        "instrumentation_control": "Matched reference run with span recording disabled; root lifecycle, hashing and fixture tokenization remain. Batch prompt capture is also disabled in this control. This is a partial recording-cost comparison, not total SDK overhead.",
        "manual_effort": {
            "operator_minutes": None,
            "status": "not_timed",
            "orchestration_steps": [
                "freeze protocol",
                "execute/import evidence",
                "summarize and inspect per-workload results",
            ],
        },
        "paid_provider_budget_usd": 0,
        "limits": "No new provider inference. Reference billing is synthetic fixture accounting; real self-hosted operating cost stays unknown. Do not credit new diagnoses with historical candidate outcomes or fit combined-intervention effects per finding.",
    }
    value["sha256"] = fingerprint(value)
    write_new(path, value)
    return value


def load_protocol(path, *, verify_sources=True):
    value = read(path)
    owned = dict(value)
    expected = owned.pop("sha256", None)
    if value.get("schema_version") != "1.0" or expected != fingerprint(owned):
        raise ValueError("benchmark protocol hash/version mismatch")
    if verify_sources:
        for name, expected in value["analyzer"]["sources"].items():
            candidate = (ROOT / name).resolve()
            if (
                not name.startswith(("agentloop/", "examples/"))
                or not name.endswith(".py")
                or not candidate.is_relative_to(ROOT.resolve())
                or source_hash(candidate) != expected
            ):
                raise ValueError("benchmark source differs from the frozen protocol")
    return value
