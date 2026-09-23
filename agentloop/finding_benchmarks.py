"""Evaluate canonical findings against a frozen corpus without executing judges."""

from __future__ import annotations

import json
import platform
from collections import Counter, defaultdict
from time import perf_counter_ns

from agentloop import rules
from agentloop.finding_benchmark_types import FINDING_BENCHMARK_VERSION, FindingBenchmark
from agentloop.judgment_types import canonical, fingerprint, finite, text
from agentloop.markdown import markdown_table_cell
from agentloop.semantic_waste import read_semantic_waste
from agentloop.timing import event_interval_ms
from agentloop.tracer import AgentTrace


def _copy(value):
    return json.loads(canonical(value))


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def _evidence(case, trace):
    semantic = read_semantic_waste(trace)
    selected = [
        row
        for row in (semantic or {}).get("cases", [])
        if row["definition"]["family"] == case["rule_id"]
    ]
    checks = {
        "timing": bool(trace.events)
        and all(event_interval_ms(event) is not None for event in trace.events),
        "tokens": bool(trace.events)
        and all(
            event.token_provenance in {"provider", "tokenizer", "user_supplied"}
            for event in trace.events
            if event.event_type == "model_call"
        ),
        "cost": all(
            finite(event.metadata.get("provider_reported_cost_usd"))
            and event.metadata["provider_reported_cost_usd"] >= 0
            for event in trace.events
            if event.event_type == "model_call"
        ),
        "semantic": bool(selected)
        and all(row["status"] in {"supported", "not_supported", "retained"} for row in selected),
    }
    backends = {}
    for row in selected:
        for judgment in row["judgments"]:
            identity = judgment["judge"]
            backends[fingerprint(identity)] = identity
    return {
        "required": case["required_evidence"],
        "missing": [key for key in case["required_evidence"] if not checks[key]],
        "accounting": "recorded timing; exact token provenance; reported model billing; validated saved semantic investigations",
        "semantic_statuses": [
            {
                "id": row["definition"]["investigation_id"],
                "status": row["status"],
                "basis": row["basis"],
            }
            for row in selected
        ],
        "backends": [backends[key] for key in sorted(backends)],
    }


def run_finding_benchmark(protocol: FindingBenchmark, *, split, source_revision):
    """Analyze each selected native trace once. No callbacks, retries or tuning.

    Timings include native report construction and all canonical rules, not an
    invented allocation of that time to individual rules. Only the case's labeled
    rule is scored; other rules' outputs are outside that case's label scope.
    """
    if type(protocol) is not FindingBenchmark:
        raise ValueError("a frozen FindingBenchmark is required")
    text(source_revision, "source_revision")
    cases = protocol.cases(split)
    registry = {rule.rule_id: rule.version for rule in rules.BUILTIN_RULES}
    identities = {case["rule_id"] for case in cases}
    if not identities <= registry.keys():
        raise ValueError("corpus refers to an unavailable rule")
    rows = []
    for case in cases:
        trace = AgentTrace.from_dict(_copy(case["trace"]))
        start = perf_counter_ns()
        try:
            report = trace.report()
            findings = [
                item for item in report["finding_candidates"] if item["rule_id"] == case["rule_id"]
            ]
            errors = [item for item in report["rule_errors"] if item["rule_id"] == case["rule_id"]]
            status = "error" if errors else "emitted" if findings else "abstained"
        except Exception:
            # Keep failed analyses in the denominator without publishing payloads.
            findings, status = [], "error"
        elapsed = (perf_counter_ns() - start) / 1_000_000
        rows.append(
            {
                "case_id": case["id"],
                "rule_id": case["rule_id"],
                "rule_version": registry[case["rule_id"]],
                "trace_hash": fingerprint(case["trace"]),
                "status": status,
                "findings": _copy(findings),
                "analysis_ms": elapsed,
                "evidence": _evidence(case, trace),
            }
        )
    result = {
        "schema_version": FINDING_BENCHMARK_VERSION,
        "corpus_hash": protocol.corpus_hash,
        "split": split,
        "policy": protocol.to_dict()["policy"],
        "source_revision": source_revision,
        "python": platform.python_version(),
        "rules": {key: registry[key] for key in sorted(identities)},
        "rows": rows,
    }
    return {**result, "summary": summarize_finding_benchmark(protocol, result)}


def _validated_rows(protocol, result):
    if (
        result.get("schema_version") != FINDING_BENCHMARK_VERSION
        or result.get("corpus_hash") != protocol.corpus_hash
    ):
        raise ValueError("result does not match the frozen corpus")
    if result.get("policy") != protocol.to_dict()["policy"]:
        raise ValueError("frozen policy mismatch")
    text(result.get("source_revision"), "source_revision")
    cases = protocol.cases(result.get("split"))
    versions = result.get("rules")
    if not isinstance(versions, dict) or set(versions) != {case["rule_id"] for case in cases}:
        raise ValueError("result must declare every planned rule")
    for version in versions.values():
        text(version, "rule version")
    rows = result.get("rows")
    if not isinstance(rows, list):
        raise ValueError("result rows must be a list")
    by_id, expected = {}, {case["id"]: case for case in cases}
    for row in rows:
        identity = row.get("case_id")
        if identity not in expected or identity in by_id:
            raise ValueError("duplicate or unexpected result case")
        case = expected[identity]
        if (
            row.get("trace_hash") != fingerprint(case["trace"])
            or row.get("rule_id") != case["rule_id"]
            or row.get("rule_version") != versions[case["rule_id"]]
        ):
            raise ValueError("result provenance mismatch")
        status, findings = row.get("status"), row.get("findings")
        if status not in {"error", "abstained", "emitted"} or not isinstance(findings, list):
            raise ValueError("invalid result status or findings")
        if status != "error" and bool(findings) != (status == "emitted"):
            raise ValueError("result status contradicts its findings")
        if not finite(row.get("analysis_ms")) or row["analysis_ms"] < 0:
            raise ValueError("analysis timing must be finite and nonnegative")
        for finding in findings:
            if (
                finding.get("rule_id") != row["rule_id"]
                or finding.get("rule_version") != row["rule_version"]
            ):
                raise ValueError("finding rule identity mismatch")
            text(finding.get("type"), "finding family")
            spans = finding.get("affected_nodes")
            if not isinstance(spans, list) or any(not isinstance(span, str) for span in spans):
                raise ValueError("finding spans must be strings")
            text(finding.get("confidence"), "finding confidence")
        # Reconstruct evidence completeness and backend provenance from the frozen
        # trace, never from a mutable summary or caller-supplied claimed score.
        row = {**row, "evidence": _evidence(case, AgentTrace.from_dict(_copy(case["trace"])))}
        by_id[identity] = row
    return [
        (
            case,
            by_id.get(
                case["id"],
                {
                    "case_id": case["id"],
                    "rule_id": case["rule_id"],
                    "rule_version": versions[case["rule_id"]],
                    "status": "missing",
                    "findings": [],
                    "analysis_ms": None,
                    "evidence": _evidence(case, AgentTrace.from_dict(_copy(case["trace"]))),
                },
            ),
        )
        for case in cases
    ]


def _score(case, row):
    known = case["label"] in {"opportunity", "no_opportunity"}
    expected = {(item["family"], tuple(sorted(item["spans"]))) for item in case["opportunities"]}
    matched, scored = set(), []
    for finding in row["findings"]:
        signature = (finding["type"], tuple(sorted(finding["affected_nodes"])))
        correct = signature in expected and signature not in matched if known else None
        if correct:
            matched.add(signature)
        observations = finding.get("observations") or {}
        scored.append(
            {
                "confidence": finding["confidence"],
                "basis": observations.get("basis", "deterministic_rule"),
                "correct": correct,
            }
        )
    return scored, len(expected - matched)


def _aggregate(pairs):
    counts = Counter(
        {
            key: 0
            for key in (
                "planned_cases",
                "emitted_cases",
                "abstained_cases",
                "error_cases",
                "missing_cases",
                "opportunity_cases",
                "negative_cases",
                "unknown_cases",
                "ambiguous_cases",
                "negative_cases_with_findings",
                "true_positive_findings",
                "false_positive_findings",
                "unscored_findings",
                "missed_opportunities",
                "complete_evidence_cases",
                "incomplete_evidence_findings",
            )
        }
    )
    confidence, timings, backends = defaultdict(Counter), [], {}
    for case, row in pairs:
        counts["planned_cases"] += 1
        counts[f"{row['status']}_cases"] += 1
        counts[
            {
                "opportunity": "opportunity_cases",
                "no_opportunity": "negative_cases",
                "unknown": "unknown_cases",
                "ambiguous": "ambiguous_cases",
            }[case["label"]]
        ] += 1
        counts["complete_evidence_cases"] += not row["evidence"]["missing"]
        if row["evidence"]["missing"]:
            counts["incomplete_evidence_findings"] += len(row["findings"])
        counts["negative_cases_with_findings"] += case["label"] == "no_opportunity" and bool(
            row["findings"]
        )
        scored, missed = _score(case, row)
        counts["missed_opportunities"] += missed
        for finding in scored:
            key = (
                "unscored_findings"
                if finding["correct"] is None
                else "true_positive_findings"
                if finding["correct"]
                else "false_positive_findings"
            )
            counts[key] += 1
            confidence[(finding["basis"], finding["confidence"])][key] += 1
        if row["analysis_ms"] is not None:
            timings.append(row["analysis_ms"])
        for backend in row["evidence"]["backends"]:
            backends[fingerprint(backend)] = backend
    return {
        **counts,
        "precision": _ratio(
            counts["true_positive_findings"],
            counts["true_positive_findings"] + counts["false_positive_findings"],
        ),
        "false_positive_rate": _ratio(
            counts["negative_cases_with_findings"], counts["negative_cases"]
        ),
        "coverage": _ratio(counts["emitted_cases"], counts["planned_cases"]),
        "abstention_rate": _ratio(counts["abstained_cases"], counts["planned_cases"]),
        "evidence_completeness": _ratio(counts["complete_evidence_cases"], counts["planned_cases"]),
        "confidence_counts": [
            {"basis": key[0], "confidence": key[1], **dict(value)}
            for key, value in sorted(confidence.items())
        ],
        "confidence_semantics": "ordinal per rule; not probabilities; numeric calibration unavailable",
        "analysis_timing": {
            "scope": "native report including all canonical rules per selected trace",
            "measured_cases": len(timings),
            "total_ms": sum(timings),
            "mean_ms": _ratio(sum(timings), len(timings)),
        },
        "backends": [backends[key] for key in sorted(backends)],
    }


def summarize_finding_benchmark(protocol, result):
    pairs = _validated_rows(protocol, result)
    grouped = defaultdict(list)
    for case, row in pairs:
        grouped[(row["rule_id"], row["rule_version"])].append((case, row))
    overall = _aggregate(pairs)
    # The same word may represent unrelated evidence policies across rules.
    overall["confidence_counts"] = []
    overall["confidence_semantics"] = (
        "See per-rule and evidence-basis strata; confidence is never pooled across rules."
    )
    return {
        "synthetic": protocol.to_dict()["synthetic"],
        "unit": "exact family and affected-span finding; FPR is any emission per known-negative case",
        "overall": overall,
        "per_rule": [
            {"rule_id": identity, "rule_version": version, **_aggregate(group)}
            for (identity, version), group in sorted(grouped.items())
        ],
        "limits": "Corpus-specific diagnostics, not universal precision or production savings. Abstention means no emitted finding, not a proven negative. Unknown and ambiguous labels are not correctness trials. Timings include saved evidence validation, not judge inference.",
    }


def compare_finding_benchmarks(protocol, baseline, candidate, *, review_ref=None):
    """Strict zero-regression gate on the same frozen evaluation split.

    A review reference acknowledges a regression, not missing/invalid evidence.
    It is a recorded host declaration, not an authentication mechanism.
    """
    if baseline.get("split") != "evaluation" or candidate.get("split") != "evaluation":
        raise ValueError("release gates require the held-out evaluation split")
    before = summarize_finding_benchmark(protocol, baseline)
    after = summarize_finding_benchmark(protocol, candidate)
    if review_ref is not None:
        text(review_ref, "review_ref")
    old = {row["rule_id"]: row for row in before["per_rule"]}
    changes, incomplete = [], []
    for row in after["per_rule"]:
        previous = old[row["rule_id"]]
        for key in ("error_cases", "missing_cases"):
            if row[key] or previous[key]:
                incomplete.append({"rule_id": row["rule_id"], "metric": key})
        for key in (
            "false_positive_findings",
            "negative_cases_with_findings",
            "missed_opportunities",
            "unscored_findings",
            "incomplete_evidence_findings",
        ):
            if row[key] > previous[key]:
                changes.append(
                    {
                        "rule_id": row["rule_id"],
                        "baseline_version": previous["rule_version"],
                        "candidate_version": row["rule_version"],
                        "metric": key,
                        "baseline": previous[key],
                        "candidate": row[key],
                    }
                )
    return {
        "passed": not incomplete and (not changes or review_ref is not None),
        "regressions": changes,
        "incomplete": incomplete,
        "review_ref": review_ref,
        "review_applied": bool(changes and review_ref and not incomplete),
        "corpus_hash": protocol.corpus_hash,
        "policy": protocol.to_dict()["policy"],
        "baseline_revision": baseline["source_revision"],
        "candidate_revision": candidate["source_revision"],
        "threshold_policy": "No increase in false positives, missed opportunities, unscored emissions or findings with incomplete required evidence; errors/missing cases always fail.",
    }


def finding_benchmark_markdown(protocol, result, gate=None):
    summary = summarize_finding_benchmark(protocol, result)
    lines = [
        "# Finding benchmark",
        "",
        summary["limits"],
        "",
        f"Corpus: `{protocol.corpus_hash}`",
        "",
        "| Rule | Version | Cases | TP | FP | Unknown/ambiguous | Abstentions | Precision | FPR |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in summary["per_rule"]:
        values = [
            row["rule_id"],
            row["rule_version"],
            row["planned_cases"],
            row["true_positive_findings"],
            row["false_positive_findings"],
            row["unknown_cases"] + row["ambiguous_cases"],
            row["abstained_cases"],
            row["precision"],
            row["false_positive_rate"],
        ]
        lines.append(
            "| "
            + " | ".join(
                markdown_table_cell("unavailable" if value is None else str(value))
                for value in values
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "Confidence labels are ordinal within each rule; they are not calibrated probabilities. JSON retains coverage, evidence completeness, backend/config identities, per-case findings and measured analysis overhead.",
        ]
    )
    if gate is not None:
        lines.extend(
            [
                "",
                f"Release gate: {'PASS' if gate['passed'] else 'FAIL'}; regressions: {len(gate['regressions'])}; incomplete: {len(gate['incomplete'])}; explicit review: {markdown_table_cell(gate['review_ref'] or 'none')}.",
            ]
        )
    return "\n".join(lines) + "\n"
