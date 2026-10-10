"""Finished-trial cohorts using native studies and independent quality gates."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from hashlib import sha256
from html import escape
from pathlib import Path
from typing import Any

from agentloop.integrations.harbor.trial_evidence import PAIRING_KEYS, attach_trial
from agentloop.integrations.harbor.trials import HarborImportResult, HarborOptions, import_harbor
from agentloop.integrations.harbor.verifier import ScoringContract, external_outcome
from agentloop.interoperability.artifacts import native_bytes, write_artifact
from agentloop.interoperability.cohort_evidence import (
    COHORT_KEY,
    COHORT_PAIRING_KEYS,
    cohort_record,
    read_cohort,
)
from agentloop.interoperability.contracts import external_id
from agentloop.interoperability.validation import (
    ImportLimits,
    ImportValidationError,
    indirect_path,
    load_json_artifact,
    relative_reference,
    validate_json_tree,
)
from agentloop.interventions import InterventionRecord, canonical_json
from agentloop.studies import _quantile_sorted, summarize_study, summarize_values
from agentloop.study_statistics import task_weighted_summary
from agentloop.tracer import AgentTrace


def _fail(field: str, reason: str) -> None:
    raise ImportValidationError("invalid_cohort", field, reason)


def _label(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > 512:
        _fail(field, "must be a bounded nonempty label")


def _hash(value: Any) -> str:
    return sha256(canonical_json(value).encode()).hexdigest()


def _summary(values: list) -> dict:
    result = summarize_values(values)
    ordered = sorted(value for value in values if value is not None)
    result["p50"] = result["median"]
    result["p90"] = _quantile_sorted(ordered, 0.9)
    return result


@dataclass(frozen=True)
class CohortProtocol:
    protocol_id: str
    comparison_kind: str = "system_level_confounded"
    missingness_policy: str = "indeterminate"
    minimum_runtime_improvement_pct: float = 0.0
    bootstrap_samples: int = 1000
    bootstrap_seed: int = 0
    bootstrap_confidence: float = 0.95

    def __post_init__(self) -> None:
        _label(self.protocol_id, "protocol_id")
        if (
            not isinstance(self.comparison_kind, str)
            or self.comparison_kind
            not in {
                "system_level_confounded",
                "controlled_harness_ablation",
            }
            or not isinstance(self.missingness_policy, str)
            or self.missingness_policy not in {"indeterminate", "reject"}
        ):
            _fail("protocol", "unsupported comparison/missingness policy")
        if (
            type(self.minimum_runtime_improvement_pct) not in {int, float}
            or not math.isfinite(self.minimum_runtime_improvement_pct)
            or self.minimum_runtime_improvement_pct < 0
        ):
            _fail("minimum_runtime_improvement_pct", "must be finite nonnegative")
        if (
            type(self.bootstrap_samples) is not int
            or not 1 <= self.bootstrap_samples <= 10000
            or type(self.bootstrap_seed) is not int
            or type(self.bootstrap_confidence) not in {int, float}
            or not 0 < self.bootstrap_confidence < 1
        ):
            _fail("bootstrap", "invalid task-cluster bootstrap settings")

    def uncertainty_settings(self) -> dict:
        return {
            "samples": self.bootstrap_samples,
            "seed": self.bootstrap_seed,
            "confidence": self.bootstrap_confidence,
        }


@dataclass(frozen=True)
class CohortStudyResult:
    manifest_path: Path | None
    _report_json: str

    def report(self) -> dict:
        return json.loads(self._report_json)


def _trial_metrics(receipt: dict | None) -> dict:
    metadata = receipt["source_metadata"] if receipt else {}
    usage = metadata.get("usage", {})
    phases = metadata.get("phase_timing", {})
    return {
        "runtime_ms": phases.get("agent_execution", {}).get("duration_ms"),
        "trial_runtime_ms": metadata.get("trial_wall_timing", {}).get("duration_ms"),
        "environment_setup_ms": phases.get("environment_setup", {}).get("duration_ms"),
        "agent_setup_ms": phases.get("agent_setup", {}).get("duration_ms"),
        "verifier_ms": phases.get("verifier", {}).get("duration_ms"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cached_input_tokens": usage.get("cached_input_tokens"),
        "cost_usd": usage.get("cost_usd"),
        "model_call_count": None,
        "tool_call_count": None,
        "retry_count": None,
    }


def _observation(
    result: HarborImportResult,
    row: dict,
    condition: str,
    protocol: CohortProtocol,
    index: int,
    *,
    inventory: dict,
    receipt: dict | None,
    trace_index: dict,
    inventory_sha256: str,
    inventory_identity_sha256: str,
) -> tuple[AgentTrace, dict]:
    source_identity = (
        receipt["external_identity"]
        if receipt
        else {
            "job_id": inventory["job_id"],
            "trial_id": row.get("trial_id"),
            "task_id": None,
            "task_digest": None,
        }
    )
    pairing = {key: row.get("pairing", {}).get(key) for key in PAIRING_KEYS}
    if pairing["protocol_id"] is not None and pairing["protocol_id"] != protocol.protocol_id:
        _fail("protocol_id", "cohort protocol differs from captured source protocol")
    metadata = receipt["source_metadata"] if receipt else {}
    task_id, verifier_hash = source_identity.get("task_id"), metadata.get("verifier_config_hash")
    metrics = _trial_metrics(receipt)
    trajectories = bool(row.get("trajectory_run_ids"))
    if trajectories and row["import_status"] == "imported":
        projected = [
            trace_index[run_id] for run_id in row["trajectory_run_ids"] if run_id in trace_index
        ]
        source_counts = [
            trace.metadata.get("agentloop.external", {}).get("reported_model_call_count")
            for trace in projected
        ]
        metrics["model_call_count"] = (
            sum(source_counts)
            if len(projected) == len(row["trajectory_run_ids"])
            and all(value is not None for value in source_counts)
            else None
        )
        metrics["tool_call_count"] = (
            sum(event.operation_kind == "tool" for trace in projected for event in trace.events)
            if len(projected) == len(row["trajectory_run_ids"])
            else None
        )
    usage = metrics["input_tokens"] is not None and metrics["output_tokens"] is not None
    outcome = (
        receipt["outcome"]
        if receipt
        else external_outcome(
            execution_status="unknown", verifier_status="invalid", dimensions=None
        )
    )
    source_ref = (
        receipt["source"]
        if receipt
        else {
            "artifact_reference": "harbor-inventory.json",
            "artifact_sha256": inventory_sha256,
        }
    )
    captured_id = (
        receipt["receipt_id"]
        if receipt
        else external_id(
            "receipt",
            "harbor",
            {
                "job_id": inventory["job_id"],
                "inventory_sha256": inventory_identity_sha256,
                "row": index,
            },
        )
    )
    run_id = external_id(
        "run", "harbor", {"source_receipt_id": captured_id, "scope": "cohort_observation"}
    )
    record = cohort_record(
        source_receipt=receipt,
        import_status=row["import_status"],
        trajectory_available=trajectories,
        usage_available=usage,
        missingness_policy=protocol.missingness_policy,
        condition=condition,
        task_id=task_id,
        verifier_config_hash=verifier_hash,
    )
    trace = AgentTrace(
        name="external_cohort_observation",
        run_id=run_id,
        started_at="unknown",
        metadata={
            "source": "harbor_cohort",
            "synthetic": inventory.get("synthetic") is True,
            "execution_data_present": False,
            "condition": condition,
            **pairing,
            "task_id": task_id,
            "verifier_config_hash": verifier_hash,
            COHORT_KEY: record,
        },
    )
    attach_trial(
        trace,
        {
            "receipt_id": captured_id,
            "source_result": {
                "reference": source_ref["artifact_reference"],
                "sha256": source_ref["artifact_sha256"],
            },
            "measurement": {
                "runtime_ms": metrics["runtime_ms"],
                "scope": "agent_execution",
                "input_tokens": metrics["input_tokens"],
                "output_tokens": metrics["output_tokens"],
                "cached_input_tokens": metrics["cached_input_tokens"],
                "cost_usd": metrics["cost_usd"],
                "usage_provenance": "external_reported",
                "cost_scope": "agent_context",
            },
            "outcome": outcome,
            "pairing": pairing,
        },
    )
    accepted = read_cohort(trace)["accepted"]
    facts = {
        "run_id": run_id,
        "source_receipt_id": receipt["receipt_id"] if receipt else None,
        "source_identity": source_identity,
        "trial_directory": row["trial_directory"],
        "trial_id": source_identity.get("trial_id"),
        "import_status": row["import_status"],
        "outcome": outcome,
        "accepted": accepted,
        "trajectory_available": trajectories,
        "usage_available": usage,
        "pairing": {**pairing, "task_id": task_id, "verifier_config_hash": verifier_hash},
        "metrics": metrics,
        "agent_info": metadata.get("agent_info"),
        "agent_config_hash": metadata.get("agent_config_hash"),
        "environment": metadata.get("environment"),
        "dataset": metadata.get("dataset"),
        "dataset_version": metadata.get("dataset_version"),
        "exception_info": metadata.get("exception_info"),
        "source_versions": inventory["source_versions"],
        "prediction_attribution": "unavailable unless an original finding-linked intervention record is supplied",
        "measurement_provenance": {
            "runtime_scope": "agent_execution",
            "usage": "external_reported_agent_context",
            "cost": "external_reported_agent_context",
            "provider_billing_verified": False,
            "local_estimated_usd": None,
            "self_hosted_operating_cost": None,
        },
    }
    return trace, facts


def _confounders(left: dict, right: dict, protocol: CohortProtocol) -> list[str]:
    result = []
    for field in ("agent_info", "agent_config_hash", "dataset", "dataset_version"):
        a, b = left.get(field), right.get(field)
        if a is None or b is None:
            result.append(field + "_unrecorded")
        elif a != b:
            result.append(field + "_differs")
    if protocol.comparison_kind == "system_level_confounded":
        result.append("system_level_descriptive_design")
    result.extend(
        [
            "provider_queue_cache_and_rate_limit_control_unestablished",
            "independent_held_out_design_unestablished",
        ]
    )
    return result


def _compare(
    native: dict | None,
    left: list[dict],
    right: list[dict],
    right_inventory: dict,
    protocol: CohortProtocol,
) -> dict:
    by_id = {row["run_id"]: row for row in (*left, *right)}
    pairs = []
    for pair in native["pairs"] if native else []:
        before, after = by_id[pair["baseline_run_id"]], by_id[pair["candidate_run_id"]]
        correct = (
            before["outcome"]["quality_pass"] is True and after["outcome"]["quality_pass"] is True
        )
        rejected = any(row["accepted"] is False for row in (before, after))
        complete = before["accepted"] is True and after["accepted"] is True
        a, b = before["metrics"]["runtime_ms"], after["metrics"]["runtime_ms"]
        improvement = 100 * (a - b) / a if a is not None and b is not None and a > 0 else None
        improved = (
            improvement is not None
            and b < a
            and improvement >= protocol.minimum_runtime_improvement_pct
        )
        state = (
            "rejected"
            if rejected
            else "verified_quality_preserving_improvement"
            if complete and correct and improved
            else "no_verified_improvement"
            if complete and improvement is not None
            else "indeterminate"
        )
        confounders = _confounders(before, after, protocol)
        pairs.append(
            {
                **pair,
                "state": state,
                "external_correctness_pass": correct,
                "source_acceptance_complete": complete,
                "runtime_improvement_pct": improvement,
                "confounders": confounders,
                "causal_harness_effect": "not_established",
                "source_trial_ids": {
                    "baseline": before["trial_id"],
                    "candidate": after["trial_id"],
                },
            }
        )
    unidentified = right_inventory["planned_unidentified_trials"] or 0
    denominator = len(right) + unidentified
    wins = sum(pair["state"] == "verified_quality_preserving_improvement" for pair in pairs)
    observations = [
        (pair["pairing_metadata"]["task_id"], pair["deltas"]["runtime_ms"]) for pair in pairs
    ]
    uncertainty = task_weighted_summary(observations, protocol.uncertainty_settings())
    native_unmatched = (
        native["unmatched"]
        if native
        else [
            {
                "side": side,
                "run_id": row["run_id"],
                "reason": "no_native_manifest_for_empty_condition",
            }
            for side, rows in (("baseline", left), ("candidate", right))
            for row in rows
        ]
    )
    mismatch = []
    attempt_indexes = {}
    for side, rows in (("baseline", left), ("candidate", right)):
        indexed = defaultdict(list)
        for row in rows:
            repetition = row["pairing"].get("repetition")
            if repetition is not None:
                indexed[(row["pairing"].get("task_id"), type(repetition), repetition)].append(row)
        attempt_indexes[side] = indexed
    for unmatched in native_unmatched:
        row = by_id[unmatched["run_id"]]
        repetition = row["pairing"].get("repetition")
        compatible_attempt = attempt_indexes[
            "candidate" if unmatched["side"] == "baseline" else "baseline"
        ].get((row["pairing"].get("task_id"), type(repetition), repetition), [])
        reasons = sorted(
            {
                key + "_incompatible"
                for other in compatible_attempt
                for key in COHORT_PAIRING_KEYS
                if row["pairing"].get(key) != other["pairing"].get(key)
            }
        )
        mismatch.append({**unmatched, "source_exclusion_reasons": reasons or [unmatched["reason"]]})
    return {
        "pairs": pairs,
        "paired_count": len(pairs),
        "unmatched": mismatch,
        "unpaired_count": len(mismatch),
        "quality_preserving_intervention_rate": {
            "numerator": wins,
            "denominator": denominator,
            "value": wins / denominator if denominator else None,
            "selection_policy": "all observed candidate attempts plus planned unidentified candidate attempts; unmatched/incomplete attempts cannot win",
            "unidentified_planned_attempts": unidentified,
            "interpretation": "verified wins divided by the full candidate attempt population; unknown/unpaired attempts remain in the denominator, so this is a conservative observed fraction, not an exact causal rate",
        },
        "runtime_task_uncertainty": uncertainty,
        "comparison_kind": protocol.comparison_kind,
        "controlled_design_validated": False,
        "limitations": [
            "source hashes establish supplied-byte consistency, not authenticity",
            "causal model/tool/prompt control and held-out allocation are not proven by equal labels",
            "planned unidentified tasks cannot contribute fabricated task clusters",
            "job concurrency/image cache/provider queuing may confound runtime",
        ],
        "state_counts": dict(sorted(Counter(pair["state"] for pair in pairs).items())),
    }


def write_cohort_study(
    conditions: dict[str, HarborImportResult],
    out: str | Path,
    *,
    baseline: str,
    protocol: CohortProtocol,
    name="External task-paired study",
    original_interventions: tuple[InterventionRecord, ...] = (),
) -> CohortStudyResult:
    """Materialize all observed attempts without execution spans or success invention."""
    if not isinstance(conditions, dict) or len(conditions) < 2 or baseline not in conditions:
        _fail("conditions", "at least two explicit conditions and a baseline are required")
    _label(name, "name")
    for key, value in conditions.items():
        _label(key, "condition")
        if not isinstance(value, HarborImportResult):
            _fail("conditions", "sources must be captured Harbor import results")
    if any(not isinstance(record, InterventionRecord) for record in original_interventions):
        _fail(
            "interventions", "original intervention records must use the existing native contract"
        )
    root = Path(out)
    for record in original_interventions:
        InterventionRecord.from_dict(record.to_dict())
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    paths, populations, observations = {}, {}, {}
    for index, (condition, result) in enumerate(sorted(conditions.items())):
        folder = f"condition-{index}"
        result.write(root / folder)
        inventory = result.inventory()
        inventory_serialized = canonical_json(inventory).encode()
        inventory_sha256 = sha256(inventory_serialized + b"\n").hexdigest()
        inventory_identity_sha256 = sha256(inventory_serialized).hexdigest()
        receipt_index = {item.receipt_id: item.to_dict() for item in result.trial_receipts}
        trace_index = {trace.run_id: trace for trace in result.traces}
        facts, references = [], []
        for ordinal, row in enumerate(inventory["trial_rows"]):
            trace, fact = _observation(
                result,
                row,
                condition,
                protocol,
                ordinal,
                inventory=inventory,
                receipt=receipt_index.get(row.get("receipt_id")),
                trace_index=trace_index,
                inventory_sha256=inventory_sha256,
                inventory_identity_sha256=inventory_identity_sha256,
            )
            reference = f"{folder}/observations/{trace.run_id}.json"
            write_artifact(root, reference, native_bytes(trace))
            facts.append(fact)
            references.append(reference)
        paths[condition] = references
        observations[condition] = facts
        planned_gap = inventory["planned_unidentified_trials"] or 0
        populations[condition] = {
            "observed_attempts": len(facts),
            "planned_attempts": inventory["trials_planned"],
            "planned_unidentified_attempts": planned_gap,
            "denominator": len(facts) + planned_gap,
            "import_status_counts": dict(
                sorted(Counter(fact["import_status"] for fact in facts).items())
            ),
            "execution_status_counts": dict(
                sorted(Counter(fact["outcome"]["execution_status"] for fact in facts).items())
            ),
            "verifier_status_counts": dict(
                sorted(Counter(fact["outcome"]["verifier_status"] for fact in facts).items())
            ),
            "quality_pass": sum(fact["outcome"]["quality_pass"] is True for fact in facts),
            "quality_fail": sum(fact["outcome"]["quality_pass"] is False for fact in facts),
            "quality_indeterminate": sum(fact["outcome"]["quality_pass"] is None for fact in facts)
            + planned_gap,
            "accepted": sum(fact["accepted"] is True for fact in facts),
            "rejected": sum(fact["accepted"] is False for fact in facts),
            "acceptance_indeterminate": sum(fact["accepted"] is None for fact in facts)
            + planned_gap,
            "metrics": {
                key: _summary([fact["metrics"].get(key) for fact in facts] + [None] * planned_gap)
                for key in _trial_metrics(None)
            },
            "job_timing": inventory.get("job_timing"),
            "job_concurrency": inventory.get("job_concurrency"),
            "source_versions": inventory["source_versions"],
            "trial_rows": facts,
        }
    native = None
    manifest_path = None
    if all(paths.values()):
        manifest = {
            "schema_version": "1.0",
            "name": name,
            "baseline": baseline,
            "conditions": paths,
            "pairing_keys": list(COHORT_PAIRING_KEYS),
        }
        # Native manifest/statistics semantics remain unchanged. Clustered
        # uncertainty is added by the existing shared task-weighted helper.
        write_artifact(root, "study.json", (json.dumps(manifest, indent=2) + "\n").encode())
        manifest_path = root / "study.json"
        native = summarize_study(manifest_path)
    comparisons = {
        candidate: _compare(
            native["comparisons"][candidate] if native else None,
            observations[baseline],
            observations[candidate],
            conditions[candidate].inventory(),
            protocol,
        )
        for candidate in conditions
        if candidate != baseline
    }
    report = {
        "schema_version": "1.0",
        "name": name,
        "baseline": baseline,
        "protocol": {
            "protocol_id": protocol.protocol_id,
            "comparison_kind": protocol.comparison_kind,
            "missingness_policy": protocol.missingness_policy,
            "minimum_runtime_improvement_pct": protocol.minimum_runtime_improvement_pct,
        },
        "conditions": populations,
        "comparisons": comparisons,
        "native_study": native,
        "native_study_scope": "one receipt-only observation per discovered attempt, including failures/missing trajectories; planned unidentified attempts remain count-only gaps",
        "native_manifest": "study.json" if manifest_path else None,
        "native_missing_reason": None
        if manifest_path
        else "a condition has no identified observed attempts",
        "original_intervention_records": [record.to_dict() for record in original_interventions],
        "prediction_attribution": "original supplied records preserved; no estimator attribution invented for arbitrary harness differences",
        "instrumentation_overhead": {
            "status": "unavailable",
            "reason": "no recording-disabled control supplied",
        },
        "synthetic": any(
            result.inventory().get("synthetic") is True for result in conditions.values()
        ),
    }
    serialized = canonical_json(report)
    write_artifact(root, "cohort-report.json", (serialized + "\n").encode())
    sections = [
        "<!doctype html><html lang='en'><meta charset='utf-8'><title>AgentLoop external cohort</title><body>",
        "<h1>" + escape(name) + "</h1>",
        "<p>External source evidence. Correctness, execution and source coverage are independent gates. No causal harness effect or estimator attribution is invented.</p>",
    ]
    if report["synthetic"]:
        sections.append(
            "<p>Synthetic inputs: these reports demonstrate validation and do not establish empirical performance benefits.</p>"
        )
    for condition, population in populations.items():
        sections.extend(
            [
                "<h2>" + escape(condition) + "</h2>",
                "<p>"
                + str(population["denominator"])
                + " attempts retained; "
                + str(population["accepted"])
                + " accepted, "
                + str(population["rejected"])
                + " rejected, "
                + str(population["acceptance_indeterminate"])
                + " indeterminate.</p>",
            ]
        )
    for condition, comparison in comparisons.items():
        sections.extend(
            [
                "<h2>Baseline vs " + escape(condition) + "</h2>",
                "<p>Comparison: "
                + escape(comparison["comparison_kind"])
                + "; causal harness effect not established.</p><table><caption>Independent task-quality and source-evidence gates</caption><tr><th>Task</th><th>Outcome</th><th>Runtime improvement</th></tr>",
            ]
        )
        for pair in comparison["pairs"]:
            sections.append(
                "<tr><td>"
                + escape(str(pair["pairing_metadata"]["task_id"]))
                + "</td><td>"
                + escape(pair["state"])
                + "</td><td>"
                + escape(
                    str(pair["runtime_improvement_pct"])
                    if pair["runtime_improvement_pct"] is not None
                    else "unavailable"
                )
                + "</td></tr>"
            )
        sections.append(
            "</table><p>Unpaired records: "
            + str(comparison["unpaired_count"])
            + "; planned unidentified candidate attempts: "
            + str(
                comparison["quality_preserving_intervention_rate"]["unidentified_planned_attempts"]
            )
            + ".</p>"
        )
    sections.append("</body></html>")
    write_artifact(root, "cohort-report.html", "\n".join(sections).encode())
    return CohortStudyResult(manifest_path, serialized)


def _source_directory(root: Path, reference: str) -> Path:
    reference = relative_reference(reference)
    current = root.resolve(strict=True)
    base = current
    for part in reference.split("/"):
        current = current / part
        if indirect_path(current):
            _fail("job_path", "source directories cannot traverse symlinks/junctions")
    current = current.resolve(strict=True)
    if not current.is_dir() or not current.is_relative_to(base):
        _fail("job_path", "must be a local directory under the selected source root")
    return current


def materialize_source_study(
    path: str | Path, out: str | Path, *, limits: ImportLimits = ImportLimits()
) -> CohortStudyResult:
    """Load a strict data-only source manifest; never launch a benchmark/runtime."""
    path = Path(path)
    payload = load_json_artifact(path.parent, path.name, limits=limits).payload
    validate_json_tree(payload, limits)
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "name", "baseline", "sources", "protocol"}
        or payload["schema_version"] != "1.0"
        or not isinstance(payload["sources"], dict)
    ):
        _fail("manifest", "must contain exactly the documented source manifest fields")
    if not isinstance(payload["protocol"], dict) or set(payload["protocol"]) - set(
        CohortProtocol.__dataclass_fields__
    ):
        _fail("protocol", "unsupported protocol fields")
    if "protocol_id" not in payload["protocol"]:
        _fail("protocol", "an explicit protocol identity is required")
    protocol = CohortProtocol(**payload["protocol"])
    sources = {}
    for condition, source in payload["sources"].items():
        required = {
            "system",
            "job_path",
            "scoring",
            "task_scoring",
            "trial_context",
            "expected_hashes",
            "expected_agent",
            "expected_model",
        }
        if (
            not isinstance(source, dict)
            or not required <= set(source)
            or set(source) - (required | {"synthetic"})
            or source["system"] != "harbor"
        ):
            _fail("sources", "initial source manifests require strict completed-Harbor fields")
        scoring = (
            ScoringContract.from_dict(source["scoring"]) if source["scoring"] is not None else None
        )
        if not isinstance(source["task_scoring"], dict):
            _fail("task_scoring", "must be task digest to scoring definitions")
        by_task = {
            key: ScoringContract.from_dict(value) for key, value in source["task_scoring"].items()
        }
        result = import_harbor(
            _source_directory(path.parent, source["job_path"]),
            options=HarborOptions(
                scoring=scoring,
                scoring_by_task_digest=by_task,
                condition=condition,
                protocol_id=protocol.protocol_id,
                trial_context=source["trial_context"],
                expected_hashes=source["expected_hashes"],
                synthetic_fixture=source.get("synthetic", False),
            ),
            limits=limits,
        )
        for receipt in result.trial_receipts:
            info = receipt.to_dict()["source_metadata"].get("agent_info") or {}
            model = (info.get("model_info") or {}).get("name")
            if (
                source["expected_agent"] is not None
                and source["expected_agent"] != info.get("name")
                or source["expected_model"] is not None
                and source["expected_model"] != model
            ):
                _fail(
                    "source_identity", "declared model/agent differs from recorded source evidence"
                )
        sources[condition] = result
    return write_cohort_study(
        sources, out, baseline=payload["baseline"], protocol=protocol, name=payload["name"]
    )
