"""Offline HTML analysis artifact with escaped text and no executable trace markup."""

from __future__ import annotations

import html
import json
from typing import Any

from agentloop.costs import format_cost_usd
from agentloop.harness_evidence import METADATA_KEY, HarnessEvidenceError, read_evidence
from agentloop.timing import event_interval_ms
from agentloop.tracer import AgentTrace

_CSS = """
:root{color-scheme:light;--ink:#172c37;--muted:#526675;--line:#d6e1e6;--accent:#086d79;--paper:#f4f7f8}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.55 system-ui,sans-serif}
main{max-width:1160px;margin:auto;padding:36px 24px 80px}header{border-bottom:3px solid var(--accent);padding-bottom:24px}
h1{font-size:2rem;line-height:1.2;overflow-wrap:anywhere}h2{margin-top:38px}h3{margin:0 0 12px}p{max-width:90ch}
a{color:#075b86}a:focus-visible,summary:focus-visible{outline:3px solid #b76308;outline-offset:4px}
nav{display:flex;flex-wrap:wrap;gap:18px;margin-top:18px}.eyebrow{letter-spacing:.09em;text-transform:uppercase;font-size:.8rem;color:var(--accent);font-weight:700}
.muted{color:var(--muted)}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px}
.metric,article,details.panel{background:white;border:1px solid var(--line);border-radius:9px;padding:18px}.metric strong{display:block;font-size:1.6rem}
.badge{display:inline-block;background:#e4eef2;padding:2px 8px;border-radius:4px;font-size:.8rem;margin-right:6px}
.prediction{border-left:4px solid #8a6200}.notice{padding:12px 16px;background:#fff2d8;border-left:4px solid #9c6800}
.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;background:white}th,td{text-align:left;padding:11px;border-bottom:1px solid var(--line);vertical-align:top}
th{background:#eaf0f3}caption{text-align:left;color:var(--muted);padding:8px 0}code,pre{font: .88rem/1.5 ui-monospace,monospace}code{overflow-wrap:anywhere}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#edf2f4;padding:14px;border-radius:6px}summary{cursor:pointer;font-weight:600}details{margin:12px 0}
article{margin:14px 0;scroll-margin-top:20px}tr:target{background:#fff2d8}.track{min-width:180px;background:#e8eff2;height:12px;border-radius:6px}.bar{height:12px;background:var(--accent);border-radius:6px}
dl{display:grid;grid-template-columns:minmax(140px,1fr) 3fr;gap:7px 16px}dt{color:var(--muted)}dd{margin:0;overflow-wrap:anywhere}
footer{margin-top:45px;padding-top:16px;border-top:1px solid var(--line);font-size:.85rem;color:var(--muted)}
@media(max-width:600px){main{padding:20px 12px}h1{font-size:1.65rem}dl{display:block}dd{margin-bottom:10px}}
@media print{body{background:white}main{max-width:none;padding:0}nav{display:none}article,.metric{break-inside:avoid}a{color:inherit}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
"""


def _text(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _json(value: Any) -> str:
    return (
        "<pre>" + _text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)) + "</pre>"
    )


def _definition(items: list[tuple[str, Any]]) -> str:
    return (
        "<dl>"
        + "".join(f"<dt>{_text(key)}</dt><dd>{_text(value)}</dd>" for key, value in items)
        + "</dl>"
    )


def _table(headers: list[str], rows: list[list[str]], *, caption: str) -> str:
    return (
        '<div class="table-wrap"><table><caption>'
        + _text(caption)
        + "</caption><thead><tr>"
        + "".join(f'<th scope="col">{_text(header)}</th>' for header in headers)
        + "</tr></thead><tbody>"
        + "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
        + "</tbody></table></div>"
    )


def analysis_to_html(payload: dict[str, Any], *, include_content: bool = False) -> str:
    """Render the combined `analyze` payload. All source values enter as escaped text."""
    required = {"trace", "report", "diagnosis", "optimization"}
    if (
        not isinstance(payload, dict)
        or not required <= payload.keys()
        or any(not isinstance(payload[key], dict) for key in required)
    ):
        raise ValueError("expected a combined trace, report, diagnosis, and optimization payload")
    trace = AgentTrace.from_dict(payload["trace"])
    report, diagnosis, plan = payload["report"], payload["diagnosis"], payload["optimization"]
    nodes = plan.get("graph", {}).get("nodes", [])
    anchors = {node["node_id"]: f"span-{index}" for index, node in enumerate(nodes)}
    harness_link = (
        '<a href="#harness">Harness decisions</a>' if METADATA_KEY in trace.metadata else ""
    )
    sections = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">",
        f"<title>{_text(trace.name)} — AgentLoop analysis</title><style>{_CSS}</style></head><body><main>",
        f'<header><div class="eyebrow">AgentLoop · analysis artifact</div><h1>{_text(trace.name)}</h1>',
        f'<p class="muted">Run <code>{_text(trace.run_id)}</code></p>',
        f'<nav aria-label="Report sections"><a href="#overview">Overview</a><a href="#timeline">Timeline</a><a href="#findings">Findings</a><a href="#comparison">Comparison</a>{harness_link}<a href="#metadata">Metadata</a></nav></header>',
        '<section id="overview"><h2>Recorded execution</h2>',
    ]
    if trace.metadata.get("synthetic") is True:
        sections.append(
            '<p class="notice">Synthetic trace: these results demonstrate analysis and are not application performance evidence.</p>'
        )
    if trace.metadata.get("execution_data_present") is False:
        sections.append(
            '<p class="notice">No execution spans were supplied. This artifact contains external evaluation metadata.</p>'
        )
    if not diagnosis.get("analysis_complete", True):
        sections.append(
            '<p class="notice">Analysis incomplete: one or more finding rules failed.</p>'
            + _json(diagnosis.get("rule_errors", []))
        )
    metrics = [
        ("Elapsed runtime", f"{report['total_runtime_ms'] / 1000:.3f}s"),
        ("Recorded spans", report["event_count"]),
        ("Input / output tokens", f"{report['input_tokens']} / {report['output_tokens']}"),
        (
            "Cost from supplied pricing/usage",
            format_cost_usd(
                report.get("estimated_cost_usd"),
                report.get("cost_status"),
                token_status=report.get("token_status"),
            ),
        ),
    ]
    sections.append(
        '<div class="cards">'
        + "".join(
            f'<div class="metric"><span>{_text(label)}</span><strong>{_text(value)}</strong></div>'
            for label, value in metrics
        )
        + "</div>"
    )
    sections.append(
        _definition(
            [
                ("Token basis", report.get("token_status", "unspecified")),
                ("Cost completeness", report.get("cost_status", "unknown")),
                (
                    "Cumulative span time",
                    f"{report['cumulative_span_time_ms'] / 1000:.3f}s; overlapping/nested work may exceed elapsed runtime",
                ),
            ]
        )
    )
    sections.append("</section>" + _timeline(trace, nodes, anchors))
    bottlenecks = plan.get("graph", {}).get("bottlenecks", [])
    if bottlenecks:
        rows = []
        for node in bottlenecks:
            label = _text(node["name"])
            if node["node_id"] in anchors:
                label = f'<a href="#{anchors[node["node_id"]]}">{label}</a>'
            rows.append(
                [
                    label,
                    _text(f"{node['duration_ms']:.3f} ms"),
                    _text(f"{node['runtime_share']:.1%}"),
                ]
            )
        sections.append(
            "<h2>Longest recorded spans</h2>"
            + _table(
                ["Span", "Duration", "Elapsed runtime share"],
                rows,
                caption="Bottleneck candidates from recorded durations; nested spans can overlap.",
            )
        )
    sections.append(
        '<section id="findings"><h2>Predicted improvements</h2><p>Optimizer savings are uncalibrated hypotheses. Validate a candidate run and its quality before accepting a change.</p>'
    )
    after = plan["estimated_after"]
    sections.append(
        '<article class="prediction"><h3>Predicted outcome</h3>'
        + _definition(
            [
                ("Estimated runtime", f"{after['runtime_ms'] / 1000:.3f}s"),
                ("Estimated latency reduction", f"{after['latency_reduction_pct']:.2f}%"),
                (
                    "Estimated cost",
                    format_cost_usd(after.get("estimated_cost_usd"), after.get("cost_status")),
                ),
            ]
        )
        + "<details><summary>Compatible finding selection</summary><p>Exact selection describes the chosen combination; it does not calibrate the savings predictions.</p>"
        + _json(plan.get("savings_aggregation", {}))
        + "</details></article>"
    )
    findings = diagnosis.get("findings", [])
    if not findings:
        sections.append("<p>No optimization findings were detected.</p>")
    for index, finding in enumerate(findings):
        sections.append(_finding(finding, index, anchors))
    sections.append(
        '</section><section id="comparison"><h2>Observed baseline / candidate comparison</h2>'
    )
    replay = payload.get("replay")
    if replay:
        sections.append(_comparison(replay))
    else:
        sections.append(
            "<p>No baseline was supplied. Predicted improvements above have not been verified by this report.</p>"
        )
    metadata = dict(trace.metadata)
    harness_section = ""
    if METADATA_KEY in metadata:
        metadata.pop(METADATA_KEY)
        harness_section = _harness_section(trace, anchors, include_content=include_content)
    sections.append(
        "</section>"
        + harness_section
        + '<section id="metadata"><h2>Trace and research metadata</h2><p class="notice">Metadata may contain private text or identifiers. Review it before sharing this file.</p>'
        + _json(metadata)
    )
    if include_content:
        sections.append(
            '<p class="notice">Raw prompts, outputs, errors, and event metadata are included below. Review this content before sharing.</p><details class="panel"><summary>Included event content</summary>'
            + _json([event.to_dict() for event in trace.events])
            + "</details>"
        )
    else:
        sections.append(
            "<p>Raw event text and full event metadata are omitted. Finding evidence and trace metadata remain included.</p>"
        )
    sections.append(
        "</section><footer>Generated by AgentLoop. This file contains embedded styles and requires no network connection.</footer></main></body></html>"
    )
    return "\n".join(sections) + "\n"


def _harness_section(trace: AgentTrace, anchors: dict[str, str], *, include_content: bool) -> str:
    opening = '<section id="harness"><h2>Harness decisions</h2>'
    try:
        evidence = read_evidence(trace)
    except HarnessEvidenceError:
        return (
            opening
            + '<p class="notice">Harness evidence is invalid or unsupported. Its raw content is omitted.</p></section>'
        )
    if evidence is None:
        return opening + "<p>No decision evidence was retained.</p></section>"
    records = dict(
        sorted(
            evidence["decisions"].items(),
            key=lambda item: (
                item[1]["run_id"],
                -1 if item[1]["hook_sequence"] is None else item[1]["hook_sequence"],
                item[1]["policy_order"],
                item[0],
            ),
        )
    )
    links = {identity: f"harness-decision-{index}" for index, identity in enumerate(records)}
    content = [
        opening,
        "<p>Decisions record proposed and applied controls, not proven benefits. Shadow proposals are not executed interventions. Individual policy effects remain unverified; use an intervention comparison with explicit quality gates.</p>",
    ]
    if evidence["capture_errors"]:
        content.append(
            '<p class="notice">Decision capture was incomplete.</p>'
            + _json(evidence["capture_errors"])
        )
    hook_times = {}
    for record in records.values():
        hook_times.setdefault(record["hook_id"], record["timing"]["hook_duration_ms"])
    known = [duration for duration in hook_times.values() if duration is not None]
    content.append(
        _definition(
            [
                ("Decision records", len(records)),
                ("Distinct evaluated hooks", len(hook_times)),
                (
                    "Measured cumulative hook evaluation",
                    f"{sum(known):.3f} ms across {len(known)} hooks; includes lock wait, excludes evidence serialization; concurrent hooks may overlap",
                ),
                (
                    "Model/tool usage",
                    "Policy timing is separate from model/tool spans and carries no invented token or cost usage",
                ),
            ]
        )
    )
    for identity, record in records.items():
        span = record["span_id"]
        span_link = (
            f'<a href="#{anchors[span]}">{_text(span)}</a>'
            if span in anchors
            else _text(span or "not recorded")
        )
        related = ([record["retry_of"]] if record["retry_of"] is not None else []) + record[
            "conflicting_decision_ids"
        ]
        related_links = [
            f'<a href="#{links[item]}">{_text(item)}</a>'
            if item in links
            else _text(item) + " (not retained)"
            for item in related
        ]
        content.append(
            f'<article id="{links[identity]}"><h3>{_text(record["policy_id"])} · {_text(record["outcome"])}</h3>'
            + _definition(
                [
                    ("Decision", identity),
                    ("Run / branch", f"{record['run_id']} / {record['branch_id']}"),
                    ("Policy version", record["policy_version"]),
                    ("Mode", record["mode"]),
                    ("Boundary", f"{record['boundary']}/{record['phase']}"),
                    (
                        "Requested / resolved action",
                        f"{record['requested_action']} / {record['resolved_action']}",
                    ),
                    ("Invocation attempted", record["dispatched"]),
                    ("Execution status", record["execution_status"]),
                    ("Reason", record["reason_code"]),
                    ("Diagnostic feedback", record.get("feedback") or "none"),
                    (
                        "Budget snapshot",
                        "unavailable" if record["budget_snapshot"] is None else "recorded",
                    ),
                ]
            )
            + f"<p>Parent span: {span_link}</p><p>Retry / conflict links: {', '.join(related_links) or 'none'}</p>"
            + "<details><summary>Decision provenance and timing</summary>"
            + _json(record)
            + "</details></article>"
        )
    policies = evidence["policies"]
    if not include_content:
        for snapshot in policies.values():
            snapshot["configuration"] = None
            snapshot["configuration_capture"] = "redacted"
    content.append(
        "<details><summary>Historical policy declarations</summary><p>Configuration hashes identify snapshots; fingerprints do not anonymize data. Raw configuration requires explicit capture and content inclusion.</p>"
        + _json(policies)
        + "</details></section>"
    )
    return "".join(content)


def _timeline(trace: AgentTrace, nodes: list[dict[str, Any]], anchors: dict[str, str]) -> str:
    intervals = {event.event_id: event_interval_ms(event) for event in trace.events}
    known = [interval for interval in intervals.values() if interval is not None]
    start = min((interval[0] for interval in known), default=0)
    extent = max((interval[1] for interval in known), default=start) - start
    rows = []
    for node in nodes:
        interval = intervals.get(node["node_id"])
        bar = "Timing unavailable"
        if interval is not None and extent > 0:
            offset = max(0.0, min(100.0, (interval[0] - start) / extent * 100))
            width = max(0.0, min(100 - offset, (interval[1] - interval[0]) / extent * 100))
            bar = f'<div class="track" aria-hidden="true"><div class="bar" style="margin-left:{offset:.4f}%;width:{width:.4f}%"></div></div>'
        parent = node.get("parent_id")
        parent_link = (
            f'<a href="#{anchors[parent]}">{_text(parent)}</a>'
            if parent in anchors
            else _text(parent or "root")
        )
        rows.append(
            f'<tr id="{anchors[node["node_id"]]}"><td>{_text(node["name"])}<br><code>{_text(node["node_id"])}</code></td><td>{_text(node.get("operation_kind", node["event_type"]))}</td><td>{node["duration_ms"]:.3f} ms</td><td>{bar}</td><td>{parent_link}</td></tr>'
        )
    return (
        '<section id="timeline"><h2>Execution timeline</h2><div class="table-wrap"><table><caption>Recorded span timing and parent structure. Bars show relative timestamps, not predicted savings.</caption><thead><tr><th scope="col">Span</th><th scope="col">Operation</th><th scope="col">Duration</th><th scope="col">Relative timing</th><th scope="col">Parent</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></div>"
        + ("<p>No execution spans.</p>" if not rows else "")
        + "</section>"
    )


def _finding(finding: dict[str, Any], index: int, anchors: dict[str, str]) -> str:
    savings = finding["savings"]
    evidence = finding.get("evidence_level", "inferred")
    result = f'<article class="prediction" id="finding-{index}"><span class="badge">{_text(evidence)} evidence</span><span class="badge">{_text(finding["confidence"])} confidence</span><h3>{_text(finding["title"])}</h3>'
    result += f"<p>{_text(finding.get('metadata', {}).get('why', ''))}</p>"
    result += _definition(
        [
            ("Finding ID", finding["finding_id"]),
            ("Estimated latency savings", f"{savings['estimated_latency_savings_ms']:.3f} ms"),
            ("Estimated cost savings", format_cost_usd(savings.get("estimated_cost_savings_usd"))),
            ("Proposed change", finding["rewrite"]["hint"]),
            ("Validation", finding["validation"]["acceptance_criteria"]),
        ]
    )
    links = [
        f'<a href="#{anchors[span]}">{_text(span)}</a>' if span in anchors else _text(span)
        for span in finding.get("affected_spans", [])
    ]
    result += "<p>Evidence spans: " + (", ".join(links) or "none") + "</p>"
    estimate = finding.get("estimate")
    if estimate:
        result += (
            "<details><summary>Estimator, assumptions, and observed inputs</summary>"
            + _json(estimate)
            + "</details>"
        )
    else:
        result += (
            "<details><summary>Assumptions and formula</summary>"
            + _json(
                {"assumptions": finding.get("assumptions", []), "formula": savings.get("formula")}
            )
            + "</details>"
        )
    result += (
        "<details><summary>Recorded finding evidence</summary>"
        + _json(finding.get("evidence", []))
        + "</details></article>"
    )
    return result


def _comparison(replay: dict[str, Any]) -> str:
    rows = []
    for label, key in (
        ("Run", "run_id"),
        ("Elapsed runtime (ms)", "runtime_ms"),
        ("Cost", "estimated_cost_usd"),
        ("Token basis", "token_status"),
        ("Quality score", "quality_score"),
    ):
        values = []
        for side in ("baseline", "candidate"):
            run = replay[side]
            value = run.get(key)
            if key == "estimated_cost_usd":
                value = format_cost_usd(
                    value, run.get("cost_status"), token_status=run.get("token_status")
                )
            values.append(_text("unavailable" if value is None else value))
        rows.append([_text(label), *values])
    result = _table(
        ["Recorded metric", "Baseline", "Candidate"],
        rows,
        caption="Observed trace comparison; costs retain their recorded pricing and token basis.",
    )
    deltas = replay["deltas"]
    result += "<h3>Observed deltas</h3>" + _table(
        ["Metric", "Candidate minus baseline"],
        [
            [_text(label), _text("unavailable" if deltas.get(key) is None else deltas[key])]
            for label, key in (
                ("Runtime (ms)", "runtime_ms_delta"),
                ("Cost (USD)", "cost_usd_delta"),
                ("Input tokens", "input_tokens_delta"),
                ("Output tokens", "output_tokens_delta"),
            )
        ],
        caption="Negative deltas mean lower recorded values.",
    )
    result += (
        "<p>Configured gates: <strong>"
        + ("passed" if replay["gates"]["passed"] else "failed")
        + "</strong>.</p>"
    )
    result += _table(
        ["Gate", "Result", "Detail"],
        [
            [
                _text(gate["name"]),
                "indeterminate"
                if gate.get("indeterminate")
                else ("pass" if gate["passed"] else "fail"),
                _text(gate["detail"]),
            ]
            for gate in replay["gates"]["results"]
        ],
        caption="Only configured checks determine this gate result.",
    )
    result += (
        "<details><summary>Comparison configuration and provenance</summary>"
        + _json({"gates": replay["gates"], "deltas": deltas})
        + "</details>"
    )
    result += (
        "<details><summary>Supplied quality evidence</summary><p>Quality evidence may contain raw task inputs and outputs; review it before sharing.</p>"
        + (
            _json(replay["quality"])
            if replay.get("quality") is not None
            else "<p>No quality checks were supplied.</p>"
        )
        + "</details>"
    )
    return result
