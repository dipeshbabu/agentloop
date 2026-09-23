"""Offline, loss-aware summaries of host-run harness ablations."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from fractions import Fraction
from pathlib import Path

from agentloop.ablation_protocol import (
    AblationProtocol,
    AblationValidationError,
    canonical,
    fields,
    number,
    pair_key,
    text_value,
    timestamp,
)
from agentloop.interventions import (
    InterventionRecord,
    InterventionValidationError,
)
from agentloop.markdown import markdown_code_span, markdown_heading, markdown_table_cell
from agentloop.studies import StudyValidationError, summarize_study
from agentloop.study_statistics import task_weighted_summary
from agentloop.trace_sources import TraceSource

RESOURCE_METRICS = (
    "latency_ms",
    "tokens",
    "cost_usd",
    "model_calls",
    "tool_calls",
    "retries",
    "policy_eval_ms",
)
METRICS = (
    "success",
    "quality_score",
    "provider_cost_usd",
    "calculated_cost_usd",
    *(metric for metric in RESOURCE_METRICS if metric != "cost_usd"),
)
STATUSES = frozenset({"completed", "failed", "cancelled", "timed_out", "stopped", "incomplete"})
OBSERVATION_FIELDS = frozenset(
    {
        "observation_id",
        "protocol_hash",
        "task_id",
        "repetition",
        "cache_condition",
        "condition",
        "position",
        "started_at",
        "versions",
        "reset_confirmed",
        "provider_seed",
        "status",
        "stop_reason",
        "success",
        "quality_score",
        "metrics",
        "token_status",
        "cost_status",
        "cost_basis",
        "trace_run_id",
    }
)


def _observation(value, spec, protocol_hash, frozen_at, schedule):
    fields(value, OBSERVATION_FIELDS, "observation")
    for key in (
        "observation_id",
        "protocol_hash",
        "task_id",
        "repetition",
        "cache_condition",
        "condition",
    ):
        text_value(value[key], key)
    if not isinstance(value["versions"], dict):
        raise AblationValidationError("observed versions must be a mapping")
    if type(value["reset_confirmed"]) is not bool or (
        value["success"] is not None and type(value["success"]) is not bool
    ):
        raise AblationValidationError("reset_confirmed and available success must be booleans")
    if value["provider_seed"] is not None and type(value["provider_seed"]) not in {int, str}:
        raise AblationValidationError("provider_seed must be an integer, string or null")
    if not isinstance(value["status"], str) or value["status"] not in STATUSES:
        raise AblationValidationError("unsupported observation status")
    for key in ("stop_reason", "trace_run_id"):
        if value[key] is not None:
            text_value(value[key], key)
    if value["stop_reason"] is not None and not re.fullmatch(
        r"[A-Za-z0-9_.:-]{1,96}", value["stop_reason"]
    ):
        raise AblationValidationError("stop_reason must be a bounded identifier")
    number(value["position"], "position", integer=True)
    number(value["quality_score"], "quality_score", maximum=1, nullable=True)
    fields(value["metrics"], RESOURCE_METRICS, "metrics")
    for metric, amount in value["metrics"].items():
        number(
            amount,
            metric,
            integer=metric in {"tokens", "model_calls", "tool_calls", "retries"},
            nullable=True,
        )
    for key, options in (
        ("token_status", {"exact", "empty", "estimated", "partial", "unavailable", "unspecified"}),
        ("cost_status", {"complete", "empty", "partial", "unknown"}),
        ("cost_basis", {"provider_reported", "calculated", "mixed", "unknown"}),
    ):
        if not isinstance(value[key], str) or value[key] not in options:
            raise AblationValidationError(f"unsupported {key}")
    for metric, status in (("tokens", "token_status"), ("cost_usd", "cost_status")):
        if value[status] == "empty" and value["metrics"][metric] not in {None, 0}:
            raise AblationValidationError(f"empty {status} cannot contain a positive measurement")
    issues = []
    slot = schedule.get(pair_key(value))
    if slot is None or value["condition"] not in spec["conditions"]:
        issues.append("unplanned_observation")
    elif value["position"] != slot["order"].index(value["condition"]):
        issues.append("order_mismatch")
    if value["protocol_hash"] != protocol_hash:
        issues.append("protocol_mismatch")
    if timestamp(value["started_at"]) < frozen_at:
        issues.append("observed_before_freeze")
    if value["versions"] != spec["versions"]:
        issues.append("version_mismatch")
    if not value["reset_confirmed"]:
        issues.append("reset_unconfirmed")
    if (
        value["condition"] in spec["conditions"]
        and spec["conditions"][value["condition"]]["mode"] == "uninstrumented"
        and value["trace_run_id"] is not None
    ):
        issues.append("uninstrumented_has_trace")
    metrics = dict(value["metrics"])
    metrics["success"] = float(value["success"]) if value["success"] is not None else None
    metrics["quality_score"] = value["quality_score"]
    if value["token_status"] not in {"exact", "empty"}:
        metrics["tokens"] = None
    if value["cost_status"] not in {"complete", "empty"} or value["cost_basis"] not in {
        "provider_reported",
        "calculated",
    }:
        metrics["cost_usd"] = None
    if value["cost_basis"] == "calculated" and value["token_status"] not in {"exact", "empty"}:
        metrics["cost_usd"] = None
    metrics["provider_cost_usd"] = (
        metrics["cost_usd"] if value["cost_basis"] == "provider_reported" else None
    )
    metrics["calculated_cost_usd"] = (
        metrics["cost_usd"] if value["cost_basis"] == "calculated" else None
    )
    return {"observation": value, "issues": issues, "evaluable_metrics": metrics}


def _gate(left, right, settings):
    if left["issues"] or right["issues"]:
        return "indeterminate", "protocol_deviation"
    rows = (left["observation"], right["observation"])
    if any(row["status"] != "completed" or row["success"] is False for row in rows):
        return "rejected", "incomplete_or_unsuccessful_task"
    if any(row["success"] is None or row["quality_score"] is None for row in rows):
        return "indeterminate", "missing_task_quality"
    before, after = (row["quality_score"] for row in rows)
    regression = Fraction(str(before)) - Fraction(str(after))
    if min(before, after) < settings["min_score"] or regression > Fraction(
        str(settings["max_regression"])
    ):
        return "rejected", "quality_threshold"
    return "accepted", "task_quality_preserved"


def _task_summary(cases, metric, settings, *, eligible=False):
    result = task_weighted_summary(
        (
            (
                case["pairing"]["task_id"],
                None if eligible and case["quality_gate"] != "accepted" else case["deltas"][metric],
            )
            for case in cases
        ),
        settings,
    )
    return {
        "pair_summary": result["observation_summary"],
        "task_summary": result["task_summary"],
        "interval": result["interval"],
        "planned_pair_count": result["planned_observation_count"],
        "planned_task_count": result["planned_task_count"],
    }


def _comparisons(conditions):
    modes = {
        item["mode"]: name
        for name, item in conditions.items()
        if item["mode"] in {"uninstrumented", "tracing"}
    }
    tracing = modes["tracing"]
    yield modes["uninstrumented"], tracing, "trace_overhead"
    shadows = {
        canonical(item["policies"]): name
        for name, item in conditions.items()
        if item["mode"] == "shadow"
    }
    for name, condition in sorted(conditions.items()):
        if condition["mode"] == "shadow":
            yield tracing, name, "policy_shadow_overhead"
        elif condition["mode"] == "enforce":
            yield shadows[canonical(condition["policies"])], name, "enforcement_effect"
            yield tracing, name, "total_policy_effect"


def build_ablation_report(protocol, observations):
    """Summarize supplied host observations; never run, reset or score the agent."""
    protocol = AblationProtocol.from_dict(
        protocol.to_dict() if isinstance(protocol, AblationProtocol) else protocol
    )
    spec = protocol.to_dict()["specification"]
    if not isinstance(observations, list):
        raise AblationValidationError("observations must be a list, including when empty")
    observations = json.loads(canonical(observations))
    schedule = {pair_key(slot): slot for slot in spec["schedule"]}
    protocol_hash, frozen_at = protocol.protocol_hash, timestamp(spec["frozen_at"])
    rows = [_observation(value, spec, protocol_hash, frozen_at, schedule) for value in observations]
    rows.sort(key=lambda row: row["observation"]["observation_id"])
    ids = [row["observation"]["observation_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise AblationValidationError("observation IDs must be unique")
    grouped = defaultdict(list)
    for row in rows:
        value = row["observation"]
        grouped[(pair_key(value), value["condition"])].append(row)
    comparisons = []
    for before, after, kind in _comparisons(spec["conditions"]):
        strata = defaultdict(list)
        for key, slot in sorted(schedule.items()):
            left, right = grouped[(key, before)], grouped[(key, after)]
            gate, reason = (
                "unmatched",
                "duplicate_pairing_key"
                if len(left) > 1 or len(right) > 1
                else "missing_counterpart",
            )
            deltas = dict.fromkeys(METRICS)
            if len(left) == len(right) == 1:
                gate, reason = _gate(left[0], right[0], spec["quality_gate"])
                if not left[0]["issues"] and not right[0]["issues"]:
                    for metric in METRICS:
                        a, b = (
                            left[0]["evaluable_metrics"][metric],
                            right[0]["evaluable_metrics"][metric],
                        )
                        if a is not None and b is not None:
                            deltas[metric] = b - a
            case = {
                "pairing": {
                    name: slot[name] for name in ("task_id", "repetition", "cache_condition")
                },
                "baseline_observations": [row["observation"]["observation_id"] for row in left],
                "candidate_observations": [row["observation"]["observation_id"] for row in right],
                "quality_gate": gate,
                "reason": reason,
                "deltas": deltas,
            }
            strata[(spec["tasks"][slot["task_id"]]["split"], slot["cache_condition"])].append(case)
        for (split, cache), cases in sorted(strata.items()):
            counts = Counter(case["quality_gate"] for case in cases)
            comparisons.append(
                {
                    "baseline": before,
                    "candidate": after,
                    "kind": kind,
                    "split": split,
                    "cache_condition": cache,
                    "planned_pair_count": len(cases),
                    "quality_gate_counts": {
                        key: counts[key]
                        for key in ("accepted", "rejected", "indeterminate", "unmatched")
                    },
                    "quality_acceptance_rate": counts["accepted"] / len(cases),
                    "cases": cases,
                    "descriptive_deltas": {
                        metric: _task_summary(cases, metric, spec["bootstrap"])
                        for metric in METRICS
                    },
                    "quality_preserving_deltas": {
                        metric: _task_summary(cases, metric, spec["bootstrap"], eligible=True)
                        for metric in METRICS
                    },
                }
            )
    balance = []
    for split in ("pilot", "held_out"):
        for cache in sorted({slot["cache_condition"] for slot in schedule.values()}):
            slots = [
                slot
                for slot in schedule.values()
                if spec["tasks"][slot["task_id"]]["split"] == split
                and slot["cache_condition"] == cache
            ]
            if not slots:
                continue
            positions = {name: [0] * len(spec["conditions"]) for name in spec["conditions"]}
            for slot in slots:
                for position, name in enumerate(slot["order"]):
                    positions[name][position] += 1
            balance.append(
                {
                    "split": split,
                    "cache_condition": cache,
                    "planned_position_counts": positions,
                    "position_balanced": all(
                        max(counts) - min(counts) <= 1 for counts in positions.values()
                    ),
                }
            )
    return {
        "schema_version": "1.0",
        "protocol": protocol.to_dict(),
        "observations": rows,
        "includes_synthetic_data": spec["synthetic"],
        "planned_observation_count": len(schedule) * len(spec["conditions"]),
        "observed_count": len(rows),
        "unplanned_observation_count": sum(
            "unplanned_observation" in row["issues"] for row in rows
        ),
        "status_counts": dict(
            sorted(Counter(row["observation"]["status"] for row in rows).items())
        ),
        "cost_basis_counts": dict(
            sorted(Counter(row["observation"]["cost_basis"] for row in rows).items())
        ),
        "token_status_counts": dict(
            sorted(Counter(row["observation"]["token_status"] for row in rows).items())
        ),
        "cost_status_counts": dict(
            sorted(Counter(row["observation"]["cost_status"] for row in rows).items())
        ),
        "stop_reason_counts": dict(
            sorted(
                Counter(
                    row["observation"]["stop_reason"]
                    for row in rows
                    if row["observation"]["stop_reason"] is not None
                ).items()
            )
        ),
        "order_balance": balance,
        "comparisons": comparisons,
        "interpretation": "Descriptive host observations, not causal identification. Intervals give equal weight to observed task means within each split/cache stratum; repetitions are not independent tasks. Missingness, selection, small task counts, order/carryover effects and scorer errors remain limitations. A frozen hash is not proof of preregistration or environment reset. Policy evaluation time is not end-to-end overhead. Synthetic fixtures are not empirical benefits.",
    }


def summarize_ablation(bundle_path):
    """Load an offline bundle and preserve existing study/ledger artifacts."""
    path = Path(bundle_path).resolve()
    source_paths = {path}
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
        fields(
            bundle,
            {"schema_version", "protocol", "observations", "study_manifest", "interventions"},
            "bundle",
        )
        if bundle["schema_version"] != "1.0":
            raise AblationValidationError("bundle schema_version must be 1.0")
        report = build_ablation_report(bundle["protocol"], bundle["observations"])
        manifest = bundle["study_manifest"]
        if manifest is not None:
            text_value(manifest, "study_manifest")
            source_paths.add((path.parent / manifest).resolve())
        report["trace_study"] = (
            summarize_study(path.parent / manifest) if manifest is not None else None
        )
        if report["trace_study"] is not None:
            source_paths.update(
                Path(run["path"]).resolve()
                for condition in report["trace_study"]["conditions"].values()
                for run in condition["runs"]
            )
        if not isinstance(bundle["interventions"], list):
            raise AblationValidationError("interventions must be a list of existing ledger paths")
        report["interventions"] = []
        intervention_ids = set()
        for reference in bundle["interventions"]:
            text_value(reference, "intervention path")
            source_paths.add((path.parent / reference).resolve())
            payload = json.loads((path.parent / reference).read_text(encoding="utf-8"))
            record = InterventionRecord.from_dict(payload).to_dict()
            if record["intervention_id"] in intervention_ids:
                raise AblationValidationError("duplicate intervention artifact")
            intervention_ids.add(record["intervention_id"])
            report["interventions"].append(record)
        _verify_links(report)
        report["source_paths"] = sorted(str(source) for source in source_paths)
        return report
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RecursionError,
        StudyValidationError,
        InterventionValidationError,
    ) as exc:
        raise AblationValidationError(f"cannot read ablation bundle: {exc}") from exc


def _verify_links(report):
    study = report["trace_study"]
    spec = report["protocol"]["specification"]
    referenced = [
        row["observation"]
        for row in report["observations"]
        if row["observation"]["trace_run_id"] is not None
    ]
    if study is None:
        if referenced or report["interventions"]:
            raise AblationValidationError(
                "trace/ledger references require an existing study manifest"
            )
        return
    if study["manifest"]["pairing_keys"] != ["task_id", "repetition", "cache_condition"]:
        raise AblationValidationError(
            "linked study must use task_id, repetition, cache_condition pairing"
        )
    runs = {}
    for condition, details in study["conditions"].items():
        if (
            condition not in spec["conditions"]
            or spec["conditions"][condition]["mode"] == "uninstrumented"
        ):
            raise AblationValidationError(
                "linked trace study has an undeclared or uninstrumented condition"
            )
        for run in details["runs"]:
            runs[run["run_id"]] = (condition, run)
    for observation in referenced:
        linked = runs.get(observation["trace_run_id"])
        if (
            linked is None
            or linked[0] != observation["condition"]
            or linked[1]["pairing_metadata"]
            != {key: observation[key] for key in ("task_id", "repetition", "cache_condition")}
        ):
            raise AblationValidationError(
                "observation trace reference does not match its condition/pairing key"
            )
    fingerprints = {}
    for record in report["interventions"]:
        for side in ("baseline", "candidate"):
            identity = record[f"{side}_run_id"]
            if identity not in runs:
                raise AblationValidationError(
                    "intervention references a trace outside the linked study"
                )
            if identity not in fingerprints:
                source = TraceSource.from_dict(
                    json.loads(Path(runs[identity][1]["path"]).read_text(encoding="utf-8"))
                )
                fingerprints[identity] = source.fingerprints
            if record["trace_fingerprints"][side] not in fingerprints[identity]:
                raise AblationValidationError("intervention trace fingerprint no longer matches")
    report["unlinked_trace_run_ids"] = sorted(
        set(runs) - {row["trace_run_id"] for row in referenced}
    )
    report["includes_synthetic_data"] = (
        report["includes_synthetic_data"]
        or study["includes_synthetic_data"]
        or any(record["metadata"].get("synthetic") is True for record in report["interventions"])
    )


def ablation_to_markdown(report):
    spec = report["protocol"]["specification"]
    lines = [
        f"# Ablation: {markdown_heading(spec['name'])}",
        "",
        report["interpretation"],
        "",
        f"Evidence: {'synthetic fixture' if report['includes_synthetic_data'] else 'host-supplied study; permissions and execution require external verification'}.",
        "",
        f"Planned observations: {report['planned_observation_count']}; observed: {report['observed_count']}; unplanned: {report['unplanned_observation_count']}.",
        "",
        "Execution statuses: "
        + markdown_code_span(json.dumps(report["status_counts"], sort_keys=True)),
        "",
        "Token completeness: "
        + markdown_code_span(json.dumps(report["token_status_counts"], sort_keys=True)),
        "",
        "Cost completeness / basis: "
        + markdown_code_span(
            json.dumps([report["cost_status_counts"], report["cost_basis_counts"]], sort_keys=True)
        ),
        "",
        "Stop reasons: "
        + markdown_code_span(json.dumps(report["stop_reason_counts"], sort_keys=True)),
        "",
        "Deltas are candidate minus baseline. Negative latency/cost deltas are reductions, not evidence of task success.",
        "",
        "| Comparison | Split / cache | Planned | Quality accepted | Rejected | Indeterminate | Unmatched |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in report["comparisons"]:
        counts = item["quality_gate_counts"]
        cells = [
            f"{item['baseline']} -> {item['candidate']} ({item['kind']})",
            f"{item['split']} / {item['cache_condition']}",
            item["planned_pair_count"],
            *(counts[key] for key in ("accepted", "rejected", "indeterminate", "unmatched")),
        ]
        lines.append("| " + " | ".join(markdown_table_cell(str(value)) for value in cells) + " |")
    for item in report["comparisons"]:
        lines.extend(
            [
                "",
                f"## {markdown_heading(item['baseline'])} -> {markdown_heading(item['candidate'])}: {markdown_heading(item['split'])} / {markdown_heading(item['cache_condition'])}",
                "",
                "Means give equal weight to evaluable tasks. Quality-preserving columns exclude rejected/indeterminate pairs; the planned denominator above still includes them.",
                "",
                "| Metric | Pairs: known / quality-eligible / planned | Tasks: known / quality-eligible / planned | Descriptive mean delta | Quality-preserving mean delta | Task interval (quality-preserving) |",
                "| --- | ---: | ---: | ---: | ---: | --- |",
            ]
        )
        for metric in METRICS:
            raw = item["descriptive_deltas"][metric]
            eligible = item["quality_preserving_deltas"][metric]
            interval = eligible["interval"]
            bounds = (
                f"[{interval['lower']:.6g}, {interval['upper']:.6g}]"
                if interval["status"] == "computed"
                else "insufficient tasks"
            )
            cells = [
                metric,
                f"{raw['pair_summary']['count']} / {eligible['pair_summary']['count']} / {raw['planned_pair_count']}",
                f"{raw['task_summary']['count']} / {eligible['task_summary']['count']} / {raw['planned_task_count']}",
                raw["task_summary"]["mean"],
                eligible["task_summary"]["mean"],
                bounds,
            ]
            lines.append(
                "| "
                + " | ".join(
                    markdown_table_cell("unavailable" if value is None else str(value))
                    for value in cells
                )
                + " |"
            )
    lines.extend(
        [
            "",
            "Full JSON retains every observation, protocol deviation, stop reason, duplicate/missing pair, metric denominator and task-level interval.",
            "",
        ]
    )
    return "\n".join(lines)
