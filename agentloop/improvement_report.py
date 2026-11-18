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

_TIMINGS = (("Trace latency", "trace_elapsed_ms"), ("Runner wall time", "runner_elapsed_ms"))
_MEASUREMENTS = (
    ("Input tokens", "input_tokens"),
    ("Output tokens", "output_tokens"),
    ("Model calls", "model_call_count"),
    ("Tool operations", "tool_operations"),
    ("Estimated model cost (USD)", "estimated_cost_usd"),
    ("Quality score", "quality_score"),
)
_RUNNER_CLOCKS = {"monotonic": "Monotonic clock", "capture_timestamps": "Capture timestamps"}
_TIMING_SCOPE = (
    "Trace latency covers the instrumented execution. Runner wall time spans launch "
    "through exit, including startup, trace export, and cleanup after interruption. "
    "The latency objective uses trace time."
)


def plan_markdown(specification):
    lines = [
        f"# {markdown_heading(specification['workload']['name'])}",
        "",
        "Review the baseline recommendations, apply one change, and capture the same task set again.",
        "",
        f"Investigation focus is {markdown_text(specification['focus'] or 'unavailable')}.",
        f"The measured objective is {specification['objective']}. Every baseline and candidate output must pass its declared quality checks.",
        "The frozen decision judges the objective across all pairs by median paired improvement."
        if specification.get("decision") == "aggregate"
        else "The frozen decision requires every pair to improve the objective.",
        "",
    ]
    if specification.get("verifier"):
        lines.extend([_verifier_scope(specification["verifier"]), ""])
    if (limit := specification.get("max_runner_regression_ms")) is not None:
        lines.extend(
            [f"Runner wall-time gate allows an increase of up to {limit:g} ms in each pair.", ""]
        )
    for case in specification["cases"]:
        lines.extend(
            [f"## {markdown_heading(case['task_id'])}, repetition {case['repetition']}", ""]
        )
        if verification := case.get("baseline_verifier"):
            lines.extend([f"Local verification: {markdown_text(verification['detail'])}.", ""])
        diagnosis = case["diagnosis"]
        quality = case.get("baseline_quality")
        lines.extend(
            [
                "Independent baseline quality",
                "",
                f"The declared check {'passed' if quality['passed'] else 'failed'} with score {quality['score']}. {markdown_text(quality['detail'])}"
                if quality
                else "Baseline quality evidence is unavailable.",
                "",
            ]
        )
        if diagnosis is None:
            lines.extend(
                [
                    "Baseline evidence is unavailable. This attempt remains in the result denominator.",
                    "",
                ]
            )
            continue
        lines.extend(
            [
                "Ranked investigations",
                "",
                "| Rank | Finding | Evidence | Quality risk | Estimated latency benefit (ms) | Readiness |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for candidate in diagnosis["findings"]:
            ranking = candidate.get("ranking", {})
            parts = ranking.get("components", {})
            evidence = parts.get("evidence", {}).get("value")
            risk = parts.get("quality_risk", {}).get("value")
            benefit = parts.get("latency", {}).get("value")
            lines.append(
                "| "
                + " | ".join(
                    markdown_table_cell(value)
                    for value in (
                        ranking.get("priority_rank", "unknown"),
                        candidate["title"],
                        evidence or "unknown",
                        risk or "unknown",
                        benefit if benefit is not None else "unavailable",
                        ranking.get("status", "unknown"),
                    )
                )
                + " |"
            )
        lines.append("")
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
                    "Estimated savings",
                    "",
                    "The native estimator supplies a hypothesis to test. Measured candidate outcomes are recorded by finish.",
                    "",
                    markdown_fenced_code(
                        json.dumps(
                            {
                                "estimate": finding.get("estimate"),
                                "predicted_effect": finding.get("savings"),
                            },
                            indent=2,
                        ),
                        language="json",
                    ),
                    "",
                    "Missing evidence and review requirements",
                    "",
                    ", ".join(
                        markdown_text(reason)
                        for reason in finding.get("ranking", {}).get("reasons", [])
                    )
                    or "No missing ranking inputs were reported.",
                    "",
                    "Validation and trade-offs",
                    "",
                    markdown_fenced_code(
                        json.dumps(
                            {
                                "checks": finding["validation"],
                                "assumptions": list(
                                    dict.fromkeys(
                                        [
                                            *finding.get("assumptions", []),
                                            *(finding.get("estimate") or {}).get("assumptions", []),
                                        ]
                                    )
                                ),
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


def _headline(report):
    if (report.get("decision") or {}).get("mode") == "aggregate":
        checks = (
            "per-pair checks" if report.get("runner_gate") else "correctness and completion checks"
        )
        return f"{report['passed_attempts']} of {report['planned_attempts']} pairs passed {checks}."
    return f"{report['passed_attempts']} of {report['planned_attempts']} planned attempts passed."


def comparison_label(report):
    return "Control" if report.get("comparison_baseline") == "interleaved_control" else "Baseline"


def timing_summary_rows(report):
    timing = report.get("timing_summary")
    if timing is None:
        return []
    return [
        [
            f"{label} (ms)",
            _metric(timing[key], "baseline_median_ms"),
            _metric(timing[key], "candidate_median_ms"),
            _metric(timing[key], "median_paired_delta_ms"),
            f"{timing[key]['paired_count']}/{report['planned_attempts']}",
        ]
        for label, key in _TIMINGS
    ]


def _measurement_rows(row):
    timing, replay = row.get("timing"), row["replay"]
    sides = ("baseline", "candidate")
    measurements = []
    if timing is not None:
        measurements.extend(
            [f"{label} (ms)", *(_metric(timing[side], key) for side in sides)]
            for label, key in _TIMINGS
        )
        measurements.append(
            [
                "Runner clock",
                *(
                    _RUNNER_CLOCKS.get(timing[side]["runner_clock"], "unavailable")
                    for side in sides
                ),
            ]
        )
    elif replay:
        measurements.append(
            ["Trace latency (ms)", *(_metric(replay[side], "runtime_ms") for side in sides)]
        )
    if replay:
        measurements.extend(
            [label, *(_metric(replay[side], key) for side in sides)] for label, key in _MEASUREMENTS
        )
    if verification := row.get("verifier"):
        measurements.append(["Local verifier", *(verification[side]["detail"] for side in sides)])
    return measurements


_TIMING_SUMMARY_SCOPE = (
    "Medians use pairs with completed traces, passing checks on both answers, and both "
    "measurements for the given scope. The paired difference is candidate minus "
    "comparison time for each pair, then summarized by the median."
)


def _timing_headers(comparison):
    return [
        "Timing",
        f"{comparison} median",
        "Candidate median",
        "Median paired difference",
        "Pairs measured",
    ]


def _basis(report):
    if report.get("comparison_baseline") == "interleaved_control":
        return (
            "Measurements compare the candidate with an unchanged control captured in "
            "alternating order. Findings remain frozen from the original baseline."
        )
    return (
        "Measurements compare the candidate with the frozen baseline captured before "
        "the change. Run order is not counterbalanced."
    )


def decision_objective(verdict):
    return "trace latency" if verdict["objective"] == "latency" else verdict["objective"]


def runner_gate_summary(report):
    if gate := report.get("runner_gate"):
        return (
            f"Runner wall-time gate passed on {gate['passed_pairs']} of {report['planned_attempts']} pairs. "
            f"Limit {gate['limit_ms']:g} ms more per pair. "
            f"{gate['unavailable_pairs']} pairs lacked completed monotonic timing."
        )
    return None


def _verifier_scope(verifier):
    return (
        f"Local verifier SHA-256 {verifier['sha256']}. Each execution has a "
        f"{verifier['timeout_s']:g}-second timeout. Both sides must pass."
    )


def _decision_lines(report):
    verdict = report.get("decision") or {"mode": "per_pair"}
    if verdict["mode"] != "aggregate":
        return ["Decision: every pair must pass correctness, completion and performance checks."]
    median = verdict["median_improvement_pct"]
    p_value = verdict["sign_test_p_value"]
    objective = decision_objective(verdict)
    lines = [
        "Decision: every pair must pass correctness and completion checks; "
        f"{objective} is judged across all pairs.",
        f"Aggregate {objective} check "
        + ("passed" if verdict["passed"] else "failed")
        + ": median paired improvement "
        + ("unavailable" if median is None else f"{median:.2f}%")
        + f" (required at least {verdict['required_improvement_pct']:.2f}%), "
        f"{verdict['improved_pairs']} improved, {verdict['regressed_pairs']} regressed, "
        f"{verdict['unchanged_pairs']} unchanged.",
    ]
    if p_value is not None:
        lines.append(
            f"One-sided sign test p-value {p_value:.3f}. It is reported for context and does not gate the decision."
        )
    if not verdict["secondary_judged"]:
        lines.append(
            f"Median {verdict['secondary_metric']} regression was not judged because it was unavailable for some pairs."
        )
    lines.extend(f"Aggregate check failed: {reason}" for reason in verdict["reasons"])
    return lines


def report_markdown(report):
    comparison = comparison_label(report)
    lines = [
        f"# {markdown_heading(report['name'])}",
        "",
        "The change passed the declared checks."
        if report["passed"]
        else "The change did not pass the declared checks.",
        "",
        _headline(report),
        "",
        "Applied change",
        "",
        markdown_text(report["change"]),
        "",
        _basis(report),
        "",
        *[item for line in _decision_lines(report) for item in (markdown_text(line), "")],
    ]
    if report["synthetic"]:
        lines.extend(
            [
                "This comparison includes synthetic evidence. Results apply to the supplied fixtures.",
                "",
            ]
        )
    if report.get("verifier"):
        lines.extend([_verifier_scope(report["verifier"]), ""])
    if summary := runner_gate_summary(report):
        lines.extend([summary, ""])
    timing_rows = timing_summary_rows(report)
    if timing_rows:
        headers = _timing_headers(comparison)
        lines.extend(
            [
                "## Paired timing",
                "",
                _TIMING_SCOPE,
                "",
                _TIMING_SUMMARY_SCOPE,
                "",
                "| " + " | ".join(headers) + " |",
                "| " + " | ".join(["---"] * len(headers)) + " |",
                *("| " + " | ".join(values) + " |" for values in timing_rows),
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
        measurements = _measurement_rows(row)
        if measurements:
            lines.extend(["", f"| Measurement | {comparison} | Candidate |", "| --- | --- | --- |"])
            lines.extend("| " + " | ".join(values) + " |" for values in measurements)
        if row["replay"]:
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
    comparison = comparison_label(report)
    sections = [
        f"<header><h1>{_text(report['name'])}</h1><p>{_text(_headline(report))}</p></header>",
        f"<h2>Applied change</h2><p>{_text(report['change'])}</p>",
        f"<p>{_text(_basis(report))}</p>",
        *[f"<p>{_text(line)}</p>" for line in _decision_lines(report)],
    ]
    if report["synthetic"]:
        sections.append(
            '<p class="notice">This comparison includes synthetic evidence. Results apply to the supplied fixtures.</p>'
        )
    if report.get("verifier"):
        sections.append(f"<p>{_text(_verifier_scope(report['verifier']))}</p>")
    if summary := runner_gate_summary(report):
        sections.append(f"<p>{_text(summary)}</p>")
    timing_rows = timing_summary_rows(report)
    if timing_rows:
        sections.extend(
            [
                f"<h2>Paired timing</h2><p>{_text(_TIMING_SCOPE)}</p><p>{_text(_TIMING_SUMMARY_SCOPE)}</p>",
                _table(
                    _timing_headers(comparison),
                    [[_text(cell) for cell in values] for values in timing_rows],
                    caption="Measured timing for completed, correct pairs",
                ),
            ]
        )
    for index, row in enumerate(report["cases"]):
        status = "Passed" if row["passed"] else "Rejected"
        sections.append(
            f"<section><h2>{_text(row['task_id'])}, repetition {row['repetition']}</h2><p><strong>{status}</strong></p>"
        )
        sections.extend(f"<p>{_text(reason)}</p>" for reason in row["reasons"])
        measurements = _measurement_rows(row)
        if measurements:
            sections.append(
                _table(
                    ["Measurement", comparison, "Candidate"],
                    [[_text(cell) for cell in values] for values in measurements],
                    caption="Paired measurements",
                )
            )
        if row["replay"]:
            replay = row["replay"]
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
                f'<p><a href="traces/baseline/{index:04d}.json">{comparison} trace</a> · <a href="traces/candidate/{index:04d}.json">Candidate trace</a></p>'
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
