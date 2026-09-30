"""Rebuild per-workload benchmark summaries from retained observations only."""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from pathlib import Path

from agentloop.finding_benchmark_types import FindingBenchmark
from agentloop.finding_benchmarks import summarize_finding_benchmark
from agentloop.interventions import InterventionRecord
from agentloop.markdown import markdown_table_cell
from agentloop.studies import _bootstrap, summarize_values
from agentloop.tracer import AgentTrace
from examples.usefulness_benchmark.protocol import fingerprint, read, write_new


def _verified(path):
    value = read(path)
    content = dict(value)
    if content.pop("sha256", None) != fingerprint(content):
        raise ValueError("benchmark result content hash mismatch")
    return value


def _validate_history(root, result):
    sources = root / "historical-sources/real-agent"
    for row in result["rows"]:
        record = InterventionRecord.from_dict(
            read(
                sources
                / "study"
                / (row["phase"] + "-report")
                / "interventions"
                / (row["intervention_id"] + ".json")
            )
        ).to_dict()
        if fingerprint(record) != row["original_intervention_sha256"] or any(
            row[key] != record["measured"][key]
            for key in ("baseline", "candidate", "deltas", "gates")
        ):
            raise ValueError("historical outcome differs from its original native ledger")
    for item in result["baselines"]:
        current = read(
            root
            / "historical-analysis"
            / item["phase"]
            / item["workload"]
            / (item["task_id"] + "-" + str(item["repetition"]))
            / "current-diagnosis.json"
        )
        if fingerprint(current) != item["current_prediction_sha256"]:
            raise ValueError("current historical reanalysis snapshot mismatch")


def _paired_summary(rows, key, config):
    tasks = defaultdict(list)
    for row in rows:
        value = row.get("deltas", {}).get(key)
        if value is not None:
            tasks[row["task_id"]].append(value)
    means = [statistics.fmean(values) for _, values in sorted(tasks.items())]
    return {
        "planned_pairs": len(rows),
        "observed_pairs": sum(len(values) for values in tasks.values()),
        "independent_task_count": len(tasks),
        "task_means": dict(zip(sorted(tasks), means)),
        "summary": summarize_values(means),
        "interval": _bootstrap(means, config),
        "limits": "Descriptive task-cluster resampling; repetitions are averaged within task. Few selected/public tasks, fixed candidate order and shared hardware limit general/causal claims.",
    }


def validate_reference_row(root, workload, fixture, repeat, variant, *, protocol_sha256=None):
    slot = fingerprint([workload["id"], fixture["id"], repeat])[:20]
    folder = root / "reference" / workload["id"] / slot
    directory = folder / variant
    receipt = directory / "receipt.json"
    if not receipt.exists():
        return {
            "workload": workload["id"],
            "kind": "synthetic_reference",
            "task_id": fixture["id"],
            "repetition": repeat,
            "variant": variant,
            "split": workload["split"],
            "availability": {
                "baseline": "recorded" if (folder / "baseline.json").exists() else "missing",
                "candidate": "incomplete" if (directory / "candidate.json").exists() else "missing",
            },
            "quality": {"baseline_score": None, "candidate_score": None, "passed": False},
            "deltas": {},
            "gates": {},
            "attribution": "unavailable",
        }
    row = read(receipt)
    for key, expected in (
        ("workload", workload["id"]),
        ("task_id", fixture["id"]),
        ("repetition", repeat),
        ("variant", variant),
    ):
        if row.get(key) != expected:
            raise ValueError("benchmark pair identity mismatch")
    before, after = read(folder / "baseline.json"), read(directory / "candidate.json")
    for source, condition in ((before, "baseline"), (after, variant)):
        expected_metadata = {
            "benchmark_workload": workload["id"],
            "task_id": fixture["id"],
            "repetition": repeat,
            "variant": condition,
        }
        if protocol_sha256 is not None:
            expected_metadata["benchmark_protocol"] = protocol_sha256
        if any(source["metadata"].get(key) != value for key, value in expected_metadata.items()):
            raise ValueError("reference trace context differs from its frozen protocol")
    frozen = read(folder / "prediction-receipt.json")
    if (
        row["baseline_sha256"] != fingerprint(before)
        or row["candidate_sha256"] != fingerprint(after)
        or frozen != row["prediction_receipt"]
    ):
        raise ValueError("benchmark source trace binding mismatch")
    if (
        fingerprint(read(folder / "prediction.json")) != frozen["diagnosis_sha256"]
        or fingerprint(read(folder / "selection.json")) != frozen["selection_sha256"]
    ):
        raise ValueError("benchmark frozen prediction/selection mismatch")
    if row["candidate_started_at"] < frozen["frozen_at"]:
        raise ValueError("candidate preceded its frozen predictions")
    quality, replay = read(directory / "quality.json"), read(directory / "replay.json")
    from examples.usefulness_benchmark.reference import _quality

    recomputed = _quality(
        workload["id"],
        fixture,
        AgentTrace.from_dict(before),
        AgentTrace.from_dict(after),
        fingerprint(workload["cases"]),
    )
    if quality != recomputed:
        raise ValueError("reference quality differs from the frozen scorer and outputs")
    if row["quality"] != {
        key: quality.get(key)
        for key in ("baseline_score", "candidate_score", "passed", "failed_case_count")
    } or any(row[key] != replay[key] for key in ("baseline", "candidate", "deltas", "gates")):
        raise ValueError("benchmark quality/resource summary mismatch")
    if row["intervention_id"]:
        record = InterventionRecord.from_dict(read(directory / "intervention.json")).to_dict()
        if (
            record["intervention_id"] != row["intervention_id"]
            or record["measured"] != replay
            or record["trace_fingerprints"]
            != {"baseline": fingerprint(before), "candidate": fingerprint(after)}
        ):
            raise ValueError("benchmark intervention mismatch")
    return row


def _finding_strata(root, prefix):
    path = root / (prefix + "-corpus.json")
    if not path.exists():
        return {"status": "unavailable", "workloads": []}
    source = read(path)
    parent_corpus = FindingBenchmark(source)
    parents = {}
    strata = []
    for workload in sorted({case["workload_ref"] for case in source["cases"]}):
        subset = {
            **source,
            "cases": [case for case in source["cases"] if case["workload_ref"] == workload],
        }
        corpus = FindingBenchmark(subset)
        split = subset["cases"][0]["split"]
        result_path = root / (prefix + "-" + split + ".json")
        if not result_path.exists():
            strata.append({"workload": workload, "status": "missing_results"})
            continue
        if split not in parents:
            parents[split] = read(result_path)
            # Validate each complete split once before deriving its workload strata.
            summarize_finding_benchmark(parent_corpus, parents[split])
        parent = parents[split]
        ids = {case["id"] for case in subset["cases"]}
        rules = {case["rule_id"] for case in subset["cases"]}
        result = {
            **parent,
            "corpus_hash": corpus.corpus_hash,
            "rows": [row for row in parent["rows"] if row["case_id"] in ids],
            "rules": {key: value for key, value in parent["rules"].items() if key in rules},
        }
        strata.append(
            {
                "workload": workload,
                "split": split,
                "status": "available",
                **summarize_finding_benchmark(corpus, result),
            }
        )
    return {"status": "available", "workloads": strata}


def _summaries(rows, config):
    groups = defaultdict(list)
    for row in rows:
        groups[row["kind"], row["workload"], row.get("phase", row["split"]), row["variant"]].append(
            row
        )
    summaries = []
    for (kind, workload, phase, variant), selected in sorted(groups.items()):
        known = [row for row in selected if row["availability"].get("candidate") == "recorded"]
        scored = [
            row
            for row in known
            if row["quality"].get("baseline_score") is not None
            and row["quality"].get("candidate_score") is not None
        ]
        summaries.append(
            {
                "kind": kind,
                "workload": workload,
                "phase": phase,
                "variant": variant,
                "planned_pairs": len(selected),
                "recorded_pairs": len(known),
                "quality_scored_pairs": len(scored),
                "candidate_quality_gate_passes": sum(
                    row["quality"].get("passed") is True for row in known
                ),
                "missing_candidate_quality_pairs": sum(
                    row["quality"].get("candidate_score") is None for row in selected
                ),
                "quality_regressing_pairs": sum(
                    row["quality"]["candidate_score"] < row["quality"]["baseline_score"]
                    for row in scored
                ),
                "paired_quality_not_lower": sum(
                    row["quality"]["candidate_score"] >= row["quality"]["baseline_score"]
                    for row in scored
                ),
                "baseline_quality_mean": summarize_values(
                    [row["quality"].get("baseline_score") for row in selected]
                ),
                "candidate_quality_mean": summarize_values(
                    [row["quality"].get("candidate_score") for row in selected]
                ),
                "candidate_execution_failures": sum(
                    row.get("candidate_error_spans", 0) > 0
                    or row.get("candidate_task_status", "completed") != "completed"
                    for row in known
                ),
                "unknown_cost_pairs": sum(
                    row.get("deltas", {}).get("cost_usd_delta") is None for row in selected
                ),
                "unattributed_pairs": sum(row.get("attribution") != "isolated" for row in selected),
                "runtime_change_ms": _paired_summary(selected, "runtime_ms_delta", config),
                "input_token_change": _paired_summary(selected, "input_tokens_delta", config),
                "output_token_change": _paired_summary(selected, "output_tokens_delta", config),
                "cost_change_usd": _paired_summary(selected, "cost_usd_delta", config),
                "quality_change": _paired_summary(selected, "quality_score_delta", config),
                "interpretation": "Candidate minus baseline. Non-regression can include two wrong answers; quality gate passes are separate. No combined-intervention outcome is allocated to individual findings.",
            }
        )
    return summaries


def summarize(root):
    root = Path(root)
    protocol = read(root / "protocol.json")
    supplied = dict(protocol)
    if supplied.pop("sha256") != fingerprint(supplied):
        raise ValueError("benchmark protocol hash mismatch")
    reference_rows = [
        validate_reference_row(
            root, workload, fixture, repeat, variant, protocol_sha256=protocol["sha256"]
        )
        for workload in protocol["workloads"]
        for fixture in workload["cases"]
        for repeat in range(workload["repetitions"])
        for variant in workload["variants"]
    ]
    reference = (
        _verified(root / "reference-results.json")
        if (root / "reference-results.json").exists()
        else None
    )
    historical = (
        _verified(root / "historical-results.json")
        if (root / "historical-results.json").exists()
        else None
    )
    if reference is not None and reference["rows"] != reference_rows:
        raise ValueError("reference inventory differs from its planned receipts")
    if historical is not None:
        _validate_history(root, historical)
    rows = reference_rows + (historical["rows"] if historical else [])
    emissions = []
    for kind, baselines in (
        ("synthetic_reference", reference["baselines"] if reference else []),
        ("real_archived", historical["baselines"] if historical else []),
    ):
        grouped = defaultdict(list)
        for baseline in baselines:
            grouped[baseline["workload"]].append(baseline)
        for workload, inputs in sorted(grouped.items()):
            current = [
                finding
                for item in inputs
                for finding in item.get("findings", item.get("current_findings", []))
            ]
            emissions.append(
                {
                    "kind": kind,
                    "workload": workload,
                    "observed_baselines": len(inputs),
                    "findings_by_family": dict(Counter(finding["type"] for finding in current)),
                    "selection_statuses": dict(Counter(finding["status"] for finding in current)),
                    "analysis_ms": summarize_values([item["analysis_ms"] for item in inputs]),
                    "semantic_inspection_ms": summarize_values(
                        [item.get("semantic_inspection_ms") for item in inputs]
                    ),
                    "analysis_scope": "Native diagnosis including saved-evidence validation; separately recorded reference semantic inspection precedes diagnosis.",
                    "missing_exact_usage_baselines": sum(
                        item["token_status"] not in {"exact", "empty"} for item in inputs
                    ),
                    "unknown_model_cost_baselines": sum(
                        item["cost_status"] not in {"complete", "empty"} for item in inputs
                    ),
                }
            )
    recording = []
    if reference:
        for workload in sorted({row["workload"] for row in reference["recording_overhead"]}):
            samples = [
                row for row in reference["recording_overhead"] if row["workload"] == workload
            ]
            recording.append(
                {
                    "kind": "synthetic_reference",
                    "workload": workload,
                    "paired_runs": len(samples),
                    "recording_delta_ms": summarize_values(
                        [row["recording_delta_ms"] for row in samples]
                    ),
                    "scope": protocol["instrumentation_control"],
                }
            )
    if historical:
        for workload in protocol["historical"]["workloads"]:
            samples = [
                row
                for row in historical["recording_overhead"]["rows"]
                if row["task_id"].startswith(workload + "-")
            ]
            recording.append(
                {
                    "kind": "real_archived",
                    "workload": workload,
                    "paired_runs": len(samples),
                    "recording_delta_ms": summarize_values(
                        [row.get("difference_ms") for row in samples]
                    ),
                    "direct_recording_ms": summarize_values(
                        [row.get("direct_recording_ms") for row in samples]
                    ),
                    "scope": "Original published recording measurements. Wall differences include model/warm-state variation and are not causal instrumentation estimates.",
                }
            )
    result = {
        "schema_version": "1.0",
        "protocol_sha256": protocol["sha256"],
        "analyzer": protocol["analyzer"],
        "reference_complete": reference is not None
        and all(row["availability"].get("candidate") == "recorded" for row in reference_rows),
        "historical_available": historical is not None,
        "workload_results": _summaries(rows, protocol["bootstrap"]),
        "emission_inventory": emissions,
        "finding_labels": {
            "synthetic_reference": _finding_strata(root, "reference-findings"),
            "real_archived": _finding_strata(root, "real-findings"),
        },
        "recording_overhead": recording,
        "manual_effort": protocol["manual_effort"],
        "retired_onboarding": historical["retired_onboarding"] if historical else None,
        "historical_calibration": historical["historical_calibration"] if historical else None,
        "limits": [
            protocol["limits"],
            protocol["split_policy"],
            protocol["finding_label_sampling"],
            "Finding opportunity labels describe justified scope, not a guaranteed successful intervention. Unknown/ambiguous labels are unscored.",
            "Small fixed/public task sets and fixture correlations do not support a universal precision or performance claim.",
            "Human effort was not timed; model weights/runtime binaries are not redistributed. Original archive failures and unmatched onboarding slots remain available.",
        ],
    }
    result["sha256"] = fingerprint(result)
    return result


def markdown(result):
    lines = [
        "# Cross-workload usefulness benchmark",
        "",
        "Results are separated by workload, evidence kind and candidate. There is no pooled usefulness score.",
        "",
        f"Protocol: {result['protocol_sha256']}",
        f"Analyzer: {result['analyzer']['package_version']} / {result['analyzer']['source_revision']}",
        "",
        "## Intervention outcomes",
        "",
        "Runtime/token/cost changes are candidate minus baseline; negative is a reduction. Quality gates and regressions use all planned pairs, including failures.",
        "",
        "| Kind | Workload | Phase / variant | Recorded / planned | Candidate quality gate | Quality regressions | Failures / missing quality | Runtime change: mean [95% task interval], n | Unknown cost pairs |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in result["workload_results"]:
        mean = row["runtime_change_ms"]["summary"]["mean"]
        interval = row["runtime_change_ms"]["interval"]
        timing = "unavailable" if mean is None else f"{mean:.3f} ms"
        if interval["status"] == "computed":
            timing += f" [{interval['lower']:.3f}, {interval['upper']:.3f}], n={row['runtime_change_ms']['independent_task_count']}"
        else:
            timing += "; interval unavailable"
        values = [
            row["kind"],
            row["workload"],
            row["phase"] + " / " + row["variant"],
            f"{row['recorded_pairs']}/{row['planned_pairs']}",
            f"{row['candidate_quality_gate_passes']}/{row['planned_pairs']}",
            row["quality_regressing_pairs"],
            f"{row['candidate_execution_failures']}/{row['missing_candidate_quality_pairs']}",
            timing,
            row["unknown_cost_pairs"],
        ]
        lines.append("| " + " | ".join(markdown_table_cell(str(value)) for value in values) + " |")
    lines += [
        "",
        "## Finding labels",
        "",
        "Labels score the first repetition of each task only. Opportunity labels test exact family/span scope; actual candidate quality is reported above.",
        "",
        "| Kind | Workload | Rule | Cases | TP | FP | Ambiguous / unknown cases | Abstained cases |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for kind, evidence in result["finding_labels"].items():
        for workload in evidence["workloads"]:
            for row in workload.get("per_rule", []):
                values = [
                    kind,
                    workload["workload"],
                    row["rule_id"],
                    row["planned_cases"],
                    row["true_positive_findings"],
                    row["false_positive_findings"],
                    row["ambiguous_cases"] + row["unknown_cases"],
                    row["abstained_cases"],
                ]
                lines.append(
                    "| " + " | ".join(markdown_table_cell(str(value)) for value in values) + " |"
                )
    lines += [
        "",
        "## Recording and analysis overhead",
        "",
        "| Kind | Workload | Paired recording controls | Mean recording delta (ms) | Mean current analysis (ms) |",
        "| --- | --- | --- | --- | --- |",
    ]
    lookup = {(row["kind"], row["workload"]): row for row in result["emission_inventory"]}
    for row in result["recording_overhead"]:
        analysis = lookup.get((row["kind"], row["workload"]), {}).get("analysis_ms", {}).get("mean")
        values = [
            row["kind"],
            row["workload"],
            row["paired_runs"],
            row["recording_delta_ms"]["mean"],
            analysis,
        ]
        lines.append(
            "| "
            + " | ".join(
                markdown_table_cell(
                    "unavailable"
                    if value is None
                    else f"{value:.3f}"
                    if type(value) is float
                    else str(value)
                )
                for value in values
            )
            + " |"
        )
    lines += [
        "",
        "Reference controls isolate part of span/payload recording; they retain root lifecycle, hashing and fixture tokenization. Historical real wall differences include model variation. Negative deltas are retained. Source-specific scopes and raw observations are in JSON.",
        "",
        "## Historical predictions and incomplete cases",
        "",
    ]
    if result["retired_onboarding"]:
        disposition = result["retired_onboarding"]["source_disposition"]
        lines.append(
            f"The retired onboarding protocol retains {disposition['recorded_baseline_attempts']} baseline attempts and {disposition['not_run_candidate_slots']} unexecuted candidate slots. They are separate from the revised pilot and evaluation cohorts."
        )
    calibration = result["historical_calibration"]
    if calibration:
        lines.append(
            f"The original calibration retains {calibration['registration_count']} finding registrations across {len(calibration['cohorts'])} cohorts. Its original estimator versions, selected/rejected registrations, quality-rejected outcomes and task-cluster intervals are preserved in the result bundle. No new diagnosis is assigned those historical effects, and no runtime coefficient is changed."
        )
        lines += [
            "",
            "Attributable original latency diagnostics (milliseconds; quality acceptance remains separate):",
            "",
            "| Original estimator / version | Split | Independent tasks | Predicted saving | Realized saving | Signed error [95% task interval] | Accepted quality / selected |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for cohort in calibration["cohorts"]:
            for split, metrics in cohort["metrics"].get("latency", {}).get("splits", {}).items():
                original = metrics["original"]
                observed = original["realized_savings"]["task_summary"]
                if not observed["count"]:
                    continue
                error = original["signed_error"]
                interval = error["interval"]
                estimate = error["task_summary"]["mean"]
                error_text = "unavailable" if estimate is None else f"{estimate:.3f}"
                if interval.get("lower") is not None:
                    error_text += f" [{interval['lower']:.3f}, {interval['upper']:.3f}]"
                values = [
                    cohort["estimator"]["estimator_id"]
                    + " / "
                    + cohort["estimator"]["estimator_version"],
                    split,
                    observed["count"],
                    original["predicted_savings"]["task_summary"]["mean"],
                    observed["mean"],
                    error_text,
                    f"{metrics['quality_gate_counts']['accepted']}/{metrics['selected_count']}",
                ]
                lines.append(
                    "| "
                    + " | ".join(
                        markdown_table_cell(f"{value:.3f}" if type(value) is float else str(value))
                        for value in values
                    )
                    + " |"
                )
    lines += ["", "## Limits and effort", ""] + ["- " + value for value in result["limits"]]
    lines.append(
        "- Three explicit orchestration steps are recorded; operator minutes and subjective review effort were not timed."
    )
    return "\n".join(lines) + "\n"


def export(root, json_out, markdown_out):
    result = summarize(root)
    write_new(json_out, result)
    markdown_out = Path(markdown_out)
    markdown_out.parent.mkdir(parents=True, exist_ok=True)
    with markdown_out.open("x", encoding="utf-8") as handle:
        handle.write(markdown(result))
    return result
