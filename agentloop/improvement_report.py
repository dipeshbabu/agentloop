"""Readable recommendations and paired results for guided optimization."""

from __future__ import annotations

import json

from agentloop.experiment_artifacts import write_once
from agentloop.html_report import _CSS, _table, _text
from agentloop.markdown import (
    markdown_fenced_code,
    markdown_heading,
    markdown_table_cell,
    markdown_text,
)


def plan_markdown(specification):
    lines = [
        f"# {markdown_heading(specification['workload']['name'])}",
        "",
        "Review the baseline recommendations, apply one change, and capture the same task set again.",
        "",
        f"Investigation focus is {markdown_text(specification['focus'] or 'unavailable')}.",
        f"The measured objective is {specification['objective']}. Every baseline and candidate output must pass its declared quality checks.",
        "",
    ]
    for case in specification["cases"]:
        lines.extend(
            [f"## {markdown_heading(case['task_id'])}, repetition {case['repetition']}", ""]
        )
        diagnosis = case["diagnosis"]
        if diagnosis is None:
            lines.extend(
                [
                    "Baseline evidence is unavailable. This attempt remains in the result denominator.",
                    "",
                ]
            )
            continue
        findings = [
            finding
            for finding in diagnosis["findings"]
            if finding["finding_id"] in case["finding_ids"]
        ]
        if not findings:
            lines.extend(["No finding in the selected family was observed for this attempt.", ""])
        for finding in findings:
            lines.extend(
                [
                    f"### {markdown_heading(finding['title'])}",
                    "",
                    f"Confidence is {markdown_text(finding['confidence'])}.",
                    "",
                    "Affected spans",
                    "",
                    ", ".join(markdown_text(span) for span in finding["affected_spans"]),
                    "",
                    "Observed evidence",
                    "",
                    markdown_fenced_code(
                        json.dumps(finding["evidence"], indent=2), language="json"
                    ),
                    "",
                    "Suggested change",
                    "",
                    markdown_text(finding["rewrite"]["hint"]),
                    "",
                    "Validation and trade-offs",
                    "",
                    markdown_fenced_code(
                        json.dumps(
                            {
                                "checks": finding["validation"],
                                "assumptions": finding.get("assumptions", []),
                            },
                            indent=2,
                        ),
                        language="json",
                    ),
                    "",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def _metric(summary, name):
    value = summary.get(name)
    if name == "estimated_cost_usd" and summary.get("has_unknown_cost"):
        return "unavailable"
    if name in {"input_tokens", "output_tokens"} and summary.get("token_status") not in {
        "exact",
        "empty",
    }:
        return "unavailable"
    if name == "tool_operations":
        value = summary.get("operation_counts", {}).get("tool", 0)
    if value is None:
        return "unavailable"
    return f"{value:.3f}" if isinstance(value, float) else str(value)


def report_markdown(report):
    lines = [
        f"# {markdown_heading(report['name'])}",
        "",
        "The change passed the declared checks."
        if report["passed"]
        else "The change did not pass the declared checks.",
        "",
        f"{report['passed_attempts']} of {report['planned_attempts']} planned attempts passed.",
        "",
        "Applied change",
        "",
        markdown_text(report["change"]),
        "",
    ]
    if report["synthetic"]:
        lines.extend(
            [
                "This comparison includes synthetic evidence. Results apply to the supplied fixtures.",
                "",
            ]
        )
    for row in report["cases"]:
        lines.extend(
            [
                f"## {markdown_heading(row['task_id'])}, repetition {row['repetition']}",
                "",
                "Passed" if row["passed"] else "Rejected",
                "",
            ]
        )
        lines.extend(f"- {markdown_text(reason)}" for reason in row["reasons"])
        if row["replay"]:
            lines.extend(["", "| Measurement | Baseline | Candidate |", "| --- | --- | --- |"])
            for label, metric in (
                ("Runtime (ms)", "runtime_ms"),
                ("Input tokens", "input_tokens"),
                ("Output tokens", "output_tokens"),
                ("Model calls", "model_call_count"),
                ("Tool operations", "tool_operations"),
                ("Estimated model cost (USD)", "estimated_cost_usd"),
                ("Quality score", "quality_score"),
            ):
                replay = row["replay"]
                lines.append(
                    f"| {label} | {_metric(replay['baseline'], metric)} | {_metric(replay['candidate'], metric)} |"
                )
            lines.extend(["", "| Gate | Result | Detail |", "| --- | --- | --- |"])
            for gate in row["replay"]["gates"]["results"]:
                status = (
                    "indeterminate"
                    if gate.get("indeterminate")
                    else "passed"
                    if gate["passed"]
                    else "failed"
                )
                lines.append(
                    f"| {markdown_table_cell(gate['name'])} | {status} | {markdown_table_cell(gate['detail'])} |"
                )
        lines.append("")
    lines.extend(
        [
            "Measurements describe this task set and run order. Repeated runs and independent tasks are needed to assess variability.",
            "",
            "Native trace snapshots, frozen findings, replay results, linked interventions and paired study data accompany this report.",
            "",
        ]
    )
    return "\n".join(lines)


def export_report(root, report):
    write_once(root, "report.json", report)
    markdown = report_markdown(report)
    (root / "report.md").write_text(markdown, encoding="utf-8")
    sections = [
        f"<header><h1>{_text(report['name'])}</h1><p>{report['passed_attempts']} of {report['planned_attempts']} planned attempts passed.</p></header>",
        f"<h2>Applied change</h2><p>{_text(report['change'])}</p>",
    ]
    if report["synthetic"]:
        sections.append(
            '<p class="notice">This comparison includes synthetic evidence. Results apply to the supplied fixtures.</p>'
        )
    measurements = (
        ("Runtime (ms)", "runtime_ms"),
        ("Input tokens", "input_tokens"),
        ("Output tokens", "output_tokens"),
        ("Model calls", "model_call_count"),
        ("Tool operations", "tool_operations"),
        ("Estimated model cost (USD)", "estimated_cost_usd"),
        ("Quality score", "quality_score"),
    )
    for index, row in enumerate(report["cases"]):
        status = "Passed" if row["passed"] else "Rejected"
        sections.append(
            f"<section><h2>{_text(row['task_id'])}, repetition {row['repetition']}</h2><p><strong>{status}</strong></p>"
        )
        sections.extend(f"<p>{_text(reason)}</p>" for reason in row["reasons"])
        if row["replay"]:
            replay = row["replay"]
            values = [
                [
                    label,
                    _text(_metric(replay["baseline"], metric)),
                    _text(_metric(replay["candidate"], metric)),
                ]
                for label, metric in measurements
            ]
            sections.append(
                _table(
                    ["Measurement", "Baseline", "Candidate"], values, caption="Paired measurements"
                )
            )
            checks = [
                [
                    _text(gate["name"]),
                    "Indeterminate"
                    if gate.get("indeterminate")
                    else "Passed"
                    if gate["passed"]
                    else "Failed",
                    _text(gate["detail"]),
                ]
                for gate in replay["gates"]["results"]
            ]
            sections.append(
                "<details><summary>Quality and performance gates</summary>"
                + _table(["Gate", "Result", "Detail"], checks, caption="Configured gate results")
                + "</details>"
            )
            sections.append(
                f'<p><a href="traces/baseline/{index:04d}.json">Baseline trace</a> · <a href="traces/candidate/{index:04d}.json">Candidate trace</a></p>'
            )
        sections.append("</section>")
    sections.append(
        '<footer><p>Measurements describe this task set and run order. Repeated runs and independent tasks are needed to assess variability.</p><a href="report.json">Complete results</a> · <a href="report.md">Markdown report</a></footer>'
    )
    page = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>AgentLoop optimization results</title><style>'
        + _CSS
        + "</style></head><body><main>"
        + "".join(sections)
        + "</main></body></html>"
    )
    (root / "report.html").write_text(page, encoding="utf-8")
