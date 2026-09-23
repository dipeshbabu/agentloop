"""Export frozen decision trials without invoking an implementation or scorer."""

from __future__ import annotations

import json
from pathlib import Path

from agentloop.markdown import markdown_heading, markdown_table_cell
from agentloop.structured_quality import attach_quality_report
from agentloop.studies import study_to_markdown, summarize_study
from agentloop.substitution_reports import (
    _copy,
    index_trials,
    load_bundle,
    paired_assessments,
    summarize_substitution_experiment,
)
from agentloop.tracer import AgentTrace


def substitution_to_markdown(report):
    lines = [
        f"# Model substitution: {markdown_heading(report['name'])}",
        "",
        report["interpretation"],
        "",
        "Measurement scope: declared decision-step usage and callback latency. Reported/calculated usage is separate from model-only profile totals; unknown values are not zero.",
        "",
    ]
    if report["synthetic"]:
        lines.extend(
            [
                "Synthetic data: these results do not establish real-model quality, latency or savings.",
                "",
            ]
        )
    lines.extend(
        [
            "## Measurements",
            "",
            "| Implementation | Planned | Unavailable output rate | Observed latency ms | Observed cost USD | Cost basis |",
            "| --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for item in report["implementations"]:
        cells = [
            item["name"],
            item["planned"],
            item["unavailable_output_rate"],
            item["measurements"]["latency_ms"]["observed_mean"],
            item["measurements"]["cost_usd"]["observed_mean"],
            json.dumps(item["cost_basis_counts"], sort_keys=True),
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
            "## Independent quality and baseline agreement",
            "",
            "A full quality decision requires every planned example and repetition. Observed quality means use completely assessed examples only. Baseline agreement is a separate descriptive metric, not independent quality evidence.",
            "",
            "| Candidate | Complete examples | Baseline quality | Candidate quality | Quality preserved on examples | Baseline agreement |",
            "| --- | ---: | ---: | ---: | --- | ---: |",
        ]
    )
    for item in report["comparisons"]:
        cells = [
            item["condition"],
            item["complete_quality_examples"],
            item["observed_baseline_quality"],
            item["observed_candidate_quality"],
            item["quality_preserved_on_examples"],
            item["baseline_agreement"]["observed_rate"],
        ]
        lines.append(
            "| "
            + " | ".join(
                markdown_table_cell("unavailable" if value is None else str(value))
                for value in cells
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def export_substitution_studies(bundle, out, *, max_invocations=10000):
    """Write native quality-bound traces and one paired study per candidate.

    Failed/unknown outputs have real trial receipts and remain in each study.
    Missing or ambiguous source records are not fabricated during export.
    Baseline executions are shared controls across these separate comparisons.
    """
    owned, examples = load_bundle(bundle, max_invocations)
    slots, extras = index_trials(owned, examples)
    pairs, pair_extras = paired_assessments(owned, slots, examples)
    if (
        extras
        or pair_extras
        or any(item["trace"] is None for item in slots.values())
        or any(item["quality"] is None for item in pairs)
    ):
        raise ValueError(
            "study export requires unambiguous receipts and saved quality assessments; failed outputs remain valid study inputs"
        )
    root = Path(out).resolve()
    documents, manifests = {}, []
    for index, name in enumerate(
        key for key in owned["implementations"] if key != owned["baseline"]
    ):
        directory = f"candidate-{index:03d}"
        conditions = {"baseline": [], "candidate": []}
        selected = [item for item in pairs if item["condition"] == name]
        for row_index, pair in enumerate(selected):
            for side, condition in (("baseline", owned["baseline"]), ("candidate", name)):
                trial = slots[condition, pair["example_id"], pair["repetition"]]
                trace = AgentTrace.from_dict(_copy(trial["trace"].to_dict()))
                attach_quality_report(trace, pair["quality"], side=side)
                filename = f"{side}-{row_index:06d}.json"
                documents[f"{directory}/{filename}"] = trace.to_dict()
                conditions[side].append(filename)
        manifest = {
            "schema_version": "1.0",
            "name": owned["protocol"]["name"] + " / " + name,
            "baseline": "baseline",
            "conditions": conditions,
            "pairing_keys": ["plan_hash", "example_id", "repetition"],
        }
        documents[f"{directory}/study.json"] = manifest
        manifests.append(root / directory / "study.json")
    comparison = summarize_substitution_experiment(owned, max_invocations=max_invocations)
    documents["bundle.json"] = owned
    documents["comparison.json"] = comparison
    encoded = {
        relative: json.dumps(value, indent=2, allow_nan=False) + "\n"
        for relative, value in documents.items()
    }
    encoded["comparison.md"] = substitution_to_markdown(comparison)
    for relative, content in encoded.items():
        target = root / relative
        if not target.resolve().is_relative_to(root):
            raise ValueError("artifact path escapes the output directory")
        if target.exists() and target.read_text(encoding="utf-8") != content:
            raise FileExistsError(
                f"refusing to replace a different experiment artifact: {relative}"
            )
    # Derived reports must also be protected. Their content is deterministic for
    # these saved traces; checking after computation never reruns a scorer.
    for manifest in manifests:
        targets = [manifest.with_name(name) for name in ("study-report.json", "study-report.md")]
        if any(not target.resolve().is_relative_to(root) for target in targets):
            raise ValueError("study report path escapes the output directory")
        if any(target.exists() for target in targets):
            if not manifest.exists():
                raise FileExistsError("orphaned study report would be overwritten")
            existing = summarize_study(manifest)
            expected = [
                json.dumps(existing, indent=2, allow_nan=False) + "\n",
                study_to_markdown(existing),
            ]
            for target, content in zip(targets, expected):
                if target.exists() and target.read_text(encoding="utf-8") != content:
                    raise FileExistsError("refusing to replace a different saved study report")
    for relative, content in encoded.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text(content, encoding="utf-8")
    reports = []
    for manifest in manifests:
        report = summarize_study(manifest)
        derived = {
            "study-report.json": json.dumps(report, indent=2, allow_nan=False) + "\n",
            "study-report.md": study_to_markdown(report),
        }
        for name, content in derived.items():
            target = manifest.with_name(name)
            if target.exists() and target.read_text(encoding="utf-8") != content:
                raise FileExistsError("refusing to replace a different saved study report")
        for name, content in derived.items():
            if not manifest.with_name(name).exists():
                manifest.with_name(name).write_text(content, encoding="utf-8")
        reports.append(report)
    return {"directory": str(root), "studies": reports, "comparison": comparison}
