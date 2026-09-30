"""Read-only experiment evidence and native store/study export adapters."""

from __future__ import annotations

import html
from collections import Counter
from fractions import Fraction
from pathlib import Path

from agentloop.ablation_protocol import timestamp
from agentloop.budget_types import Reservation, ResourceUsage, money_text
from agentloop.experiment_artifacts import (
    journal_lock,
    local_path,
    read_json,
    write_bytes_once,
    write_once,
)
from agentloop.experiment_types import canonical, fingerprint
from agentloop.experiments import slot_id
from agentloop.harness_evidence import METADATA_KEY, validate_evidence
from agentloop.interventions import InterventionRecord
from agentloop.markdown import markdown_heading, markdown_table_cell
from agentloop.structured_quality import EVIDENCE_KEY, attach_quality_report, validate_report
from agentloop.studies import summarize_study
from agentloop.tracer import AgentTrace


def read_experiment(root):
    root = Path(root).resolve()
    envelope = read_json(local_path(root, "experiment.json"))
    spec = envelope["specification"]
    identity = "experiment_" + fingerprint(spec)
    if envelope["experiment_id"] != identity or spec["schema_version"] != "1.0":
        raise ValueError("experiment declaration hash or schema changed")
    rows, planned = [], set()
    baselines = {}
    for case in spec["cases"]:
        key = fingerprint(case["case_id"])
        baseline = read_json(local_path(root, f"baselines/{key}.json"))
        diagnosis = read_json(local_path(root, f"predictions/{key}.json"))
        if (
            fingerprint(baseline) != case["baseline_hash"]
            or fingerprint(diagnosis) != case["diagnosis_hash"]
        ):
            raise ValueError("frozen baseline or prediction changed")
        if timestamp(baseline["ended_at"]) > timestamp(spec["frozen_at"]):
            raise ValueError("baseline outcome follows the frozen candidate plan")
        selected = sorted(
            (
                item
                for item in diagnosis["findings"]
                if item["finding_id"] in case["source_finding_ids"]
            ),
            key=lambda item: item["finding_id"],
        )
        if selected != case["expected_effects"] or diagnosis["run_id"] != baseline["run_id"]:
            raise ValueError("expected effects are not bound to their source baseline")
        baselines[case["case_id"]] = baseline
        for runner in spec["runners"]:
            slot = slot_id(identity, case["case_id"], runner["candidate_id"])
            planned.add(slot)
            directory = f"attempts/{slot}"
            row = {
                "slot_id": slot,
                "case_id": case["case_id"],
                "candidate_id": runner["candidate_id"],
                "status": "not_started",
                "admission": None,
                "receipt": None,
                "trace": None,
                "quality": None,
                "intervention": None,
            }
            start = local_path(root, directory + "/started.json")
            finish = local_path(root, directory + "/receipt.json")
            if start.exists():
                admission = read_json(start)
                if (
                    admission.get("experiment_id") != identity
                    or admission.get("slot_id") != slot
                    or admission.get("case_id") != case["case_id"]
                    or admission.get("candidate_id") != runner["candidate_id"]
                    or admission.get("runner") != runner
                ):
                    raise ValueError("admission record differs from frozen experiment")
                row["status"] = "unresolved"
                if timestamp(admission["started_at"]) < timestamp(spec["frozen_at"]):
                    raise ValueError("candidate admission precedes frozen expected effects")
                row["admission"] = admission
                if finish.exists():
                    receipt = read_json(finish)
                    if (
                        receipt.get("schema_version") != "1.0"
                        or receipt.get("experiment_id") != identity
                        or receipt.get("slot_id") != slot
                        or receipt.get("started_hash") != fingerprint(admission)
                        or receipt.get("status")
                        not in {"completed", "failed", "timed_out", "cancelled", "stopped"}
                        or set(receipt["artifact_hashes"])
                        != {"trace.json", "quality.json", "intervention.json"}
                    ):
                        raise ValueError("invalid experiment receipt")
                    for key, filename in (
                        ("trace", "trace.json"),
                        ("quality", "quality.json"),
                        ("intervention", "intervention.json"),
                    ):
                        value = read_json(local_path(root, directory + "/" + filename))
                        if fingerprint(value) != receipt["artifact_hashes"][filename]:
                            raise ValueError("experiment result artifact changed")
                        row[key] = value
                    trace = AgentTrace.from_dict(row["trace"])
                    if (
                        timestamp(trace.started_at) < timestamp(admission["started_at"])
                        or trace.ended_at is None
                        or timestamp(trace.ended_at) < timestamp(trace.started_at)
                    ):
                        raise ValueError("candidate timing differs from its admission")
                    validate_report(
                        row["quality"],
                        baseline_trace=AgentTrace.from_dict(baseline),
                        candidate_trace=trace,
                    )
                    record = InterventionRecord.from_dict(row["intervention"]).to_dict()
                    native = validate_evidence(trace.metadata[METADATA_KEY], trace_id=trace.run_id)
                    controls = [
                        item
                        for item in native["decisions"].values()
                        if item["call_id"] == receipt["budget_call_id"]
                        and item["harness_config_hash"] == receipt["budget_config_hash"]
                        and item["policy_id"] == "budget"
                    ]
                    if (
                        len(controls) != 2
                        or {item["phase"] for item in controls} != {"before", "after"}
                        or receipt["halted"]
                        != any(
                            item["mode"] == "enforce"
                            and item["resolved_action"] in {"stop", "escalate"}
                            for item in controls
                        )
                        or receipt["budget_dispatched"]
                        != next(item["dispatched"] for item in controls if item["phase"] == "after")
                    ):
                        raise ValueError("experiment admission budget evidence changed")
                    quality_case = row["quality"]["cases"][0]
                    execution_status = {
                        "completed": "completed",
                        "cancelled": "cancelled",
                        "timed_out": "timed_out",
                    }.get(receipt["status"], "failed")
                    if quality_case["candidate"]["execution_status"] != execution_status or receipt[
                        "invoked"
                    ] != any(event.event_id == "experiment_root_" + slot for event in trace.events):
                        raise ValueError("candidate execution state differs from observed evidence")
                    if (
                        len(row["quality"]["cases"]) != 1
                        or quality_case["case_id"] != case["case_id"]
                        or quality_case["expected_hash"] != case["expected_hash"]
                        or quality_case["baseline"]["output_hash"] != case["baseline_output_hash"]
                        or quality_case["scorer"]["configuration_hash"] != spec["scorer_hash"]
                        or (
                            receipt["status"] == "completed"
                            and quality_case["candidate"]["output_hash"] != receipt["output_hash"]
                        )
                    ):
                        raise ValueError("quality assessment differs from frozen inputs or scorer")
                    if (
                        trace.run_id != "experiment_run_" + slot
                        or trace.metadata["agentloop.experiment"]["status"] != receipt["status"]
                        or type(receipt["halted"]) is not bool
                        or type(receipt["invoked"]) is not bool
                        or type(receipt["budget_dispatched"]) is not bool
                        or record["measured"]["quality"] != row["quality"]
                        or record["measured"]["gates"]["config"] != spec["gates"]
                    ):
                        raise ValueError("experiment outcome or gate binding changed")
                    if (
                        record["trace_fingerprints"]
                        != {
                            "baseline": fingerprint(baseline),
                            "candidate": fingerprint(trace.to_dict()),
                        }
                        or record["predicted"]["findings"] != case["expected_effects"]
                        or record["configuration"]["experiment_id"] != identity
                        or record["configuration"]["slot_id"] != slot
                        or record["gates_passed"] != receipt["gates_passed"]
                        or row["quality"]["passed"] != receipt["quality_passed"]
                    ):
                        raise ValueError("native intervention is not bound to this experiment")
                    row.update(status=receipt["status"], receipt=receipt)
            elif finish.exists():
                raise ValueError("receipt exists without an admission record")
            rows.append(row)
    attempts = local_path(root, "attempts")
    if attempts.exists() and any(
        path.name not in planned for path in attempts.iterdir() if path.is_dir()
    ):
        raise ValueError("unplanned experiment attempt directory")
    return {"experiment_id": identity, "specification": spec, "baselines": baselines, "rows": rows}


def summarize_experiment(root):
    evidence = read_experiment(root)
    rows = evidence["rows"]
    reservations = [
        Reservation(**row["admission"]["runner"]["reservation"])
        for row in rows
        if row["admission"] is not None
    ]
    usages = [
        None
        if row["receipt"] is None or row["receipt"]["usage"] is None
        else ResourceUsage(**row["receipt"]["usage"])
        for row in rows
        if row["admission"] is not None
    ]
    budget_summary = {
        "scope": evidence["specification"]["budget_scope"],
        "limits": evidence["specification"]["budget"],
        "admitted_attempts": len(reservations),
        "reserved_known_tokens": sum(item.tokens for item in reservations if item.tokens_known),
        "reserved_known_cost_usd": money_text(
            sum(
                (Fraction(str(item.cost_usd)) for item in reservations if item.cost_known),
                Fraction(0),
            )
        ),
        "unknown_token_reservations": sum(not item.tokens_known for item in reservations),
        "unknown_cost_reservations": sum(not item.cost_known for item in reservations),
        "unavailable_actual_token_count": sum(
            item is None or not item.tokens_known for item in usages
        ),
        "unavailable_actual_cost_count": sum(
            item is None or not item.cost_known for item in usages
        ),
        "reservation_refunds": 0,
        "hard_spend_cap": False,
    }
    budget_summary["reported_total_tokens"] = (
        None
        if budget_summary["unavailable_actual_token_count"]
        else sum(item.tokens for item in usages)
    )
    budget_summary["reported_total_cost_usd"] = (
        None
        if budget_summary["unavailable_actual_cost_count"]
        else money_text(sum((Fraction(str(item.cost_usd)) for item in usages), Fraction(0)))
    )
    comparisons = []
    for runner in evidence["specification"]["runners"]:
        selected = [row for row in rows if row["candidate_id"] == runner["candidate_id"]]
        complete = [row for row in selected if row["receipt"] is not None]
        comparisons.append(
            {
                "candidate_id": runner["candidate_id"],
                "planned_pairs": len(selected),
                "observed_pairs": len(complete),
                "missing_pairs": len(selected) - len(complete),
                "status_counts": dict(sorted(Counter(row["status"] for row in selected).items())),
                "quality_pass_count": sum(row["receipt"]["quality_passed"] for row in complete),
                "gates_pass_count": sum(row["receipt"]["gates_passed"] for row in complete),
                "all_planned_gates_passed": len(complete) == len(selected)
                and all(row["receipt"]["gates_passed"] for row in complete),
            }
        )
    result = {
        "schema_version": "1.0",
        "experiment_id": evidence["experiment_id"],
        "name": evidence["specification"]["name"],
        "synthetic": evidence["specification"]["synthetic"],
        "planned_attempts": len(rows),
        "observed_attempts": sum(row["receipt"] is not None for row in rows),
        "status_counts": dict(sorted(Counter(row["status"] for row in rows).items())),
        "comparisons": comparisons,
        "attempts": [
            {key: row[key] for key in ("slot_id", "case_id", "candidate_id", "status", "receipt")}
            for row in rows
        ],
        "interpretation": "Linked historical baselines and caller-run candidates; every planned slot remains in the denominator. Counterfactual benefit requires the explicit independent quality and performance gates. Unknown cost is not zero. Historical baselines are not randomized contemporaneous controls.",
    }
    result["budget"] = budget_summary
    result["measurement_scope"] = {
        "native_replay_cost": "recorded_model_profile_estimate",
        "runner_usage": "separate_exclusive_caller_report",
        "latency": "protected_candidate_runner_including_lifecycle_overhead",
        "hidden_work": "not_intercepted",
    }
    result["snapshot_hash"] = fingerprint(result)
    return result


def experiment_to_markdown(report):
    lines = [
        "# Optimization experiment: " + markdown_heading(report["name"]),
        "",
        report["interpretation"],
        "",
    ]
    if report["synthetic"]:
        lines += ["Synthetic evidence; no real-workload improvement claim.", ""]
    lines += [
        "| Candidate | Planned | Observed | Missing | Quality pass | All gates passed |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in report["comparisons"]:
        values = [
            item["candidate_id"],
            item["planned_pairs"],
            item["observed_pairs"],
            item["missing_pairs"],
            item["quality_pass_count"],
            item["all_planned_gates_passed"],
        ]
        lines.append("| " + " | ".join(markdown_table_cell(str(value)) for value in values) + " |")
    lines += [
        "",
        "## Retained attempt states",
        "",
        "| Case | Candidate | State |",
        "| --- | --- | --- |",
    ]
    for item in report["attempts"]:
        lines.append(
            "| "
            + " | ".join(
                markdown_table_cell(item[key]) for key in ("case_id", "candidate_id", "status")
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def experiment_to_html(report):
    def table(headers, rows):
        return (
            "<table><thead><tr>"
            + "".join('<th scope="col">' + html.escape(value) + "</th>" for value in headers)
            + "</tr></thead><tbody>"
            + "".join(
                "<tr>"
                + "".join("<td>" + html.escape(str(value)) + "</td>" for value in row)
                + "</tr>"
                for row in rows
            )
            + "</tbody></table>"
        )

    summary = table(
        ("Candidate", "Planned", "Observed", "Missing", "Quality pass", "All gates passed"),
        (
            [
                item[key]
                for key in (
                    "candidate_id",
                    "planned_pairs",
                    "observed_pairs",
                    "missing_pairs",
                    "quality_pass_count",
                    "all_planned_gates_passed",
                )
            ]
            for item in report["comparisons"]
        ),
    )
    attempts = table(
        ("Case", "Candidate", "State"),
        (
            [item[key] for key in ("case_id", "candidate_id", "status")]
            for item in report["attempts"]
        ),
    )
    notice = (
        "<p>Synthetic evidence; no real-workload improvement claim.</p>"
        if report["synthetic"]
        else ""
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'"><title>Experiment evidence</title><style>body{font:16px/1.5 system-ui;margin:2rem auto;max-width:72rem;padding:0 1rem}table{border-collapse:collapse;width:100%}th,td{text-align:left;border-bottom:1px solid #ccc;padding:.6rem}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style></head><body><main><h1>'
        + html.escape(report["name"])
        + "</h1>"
        + notice
        + "<p>"
        + html.escape(report["interpretation"])
        + "</p><h2>Candidate comparisons</h2>"
        + summary
        + "<h2>Retained attempts</h2>"
        + attempts
        + "<details><summary>Budget and measurement scope</summary><pre>"
        + html.escape(
            canonical(
                {"budget": report["budget"], "measurement_scope": report["measurement_scope"]}
            )
        )
        + "</pre></details></main></body></html>"
    )


def export_experiment(root):
    """Export the same snapshot idempotently; never invoke a runner or scorer."""
    root = Path(root).resolve()
    with journal_lock(root):
        evidence, report = read_experiment(root), summarize_experiment(root)
        directory = "reports/" + report["snapshot_hash"]
        studies = {}
        for runner in evidence["specification"]["runners"]:
            name = runner["candidate_id"]
            selected = [row for row in evidence["rows"] if row["candidate_id"] == name]
            if not any(row["trace"] is not None for row in selected):
                studies[name] = {
                    "status": "unavailable",
                    "reason": "no_candidate_trace",
                    "planned_pairs": len(selected),
                }
                continue
            folder = directory + "/" + fingerprint(name)
            conditions = {"baseline": [], "candidate": []}
            for row in selected:
                baseline = AgentTrace.from_dict(evidence["baselines"][row["case_id"]])
                baseline.metadata["agentloop.experiment_view"] = {
                    "source_trace_hash": fingerprint(evidence["baselines"][row["case_id"]]),
                    "derived_fields": ["pairing", "quality"],
                }
                baseline.metadata.update(
                    experiment_id=evidence["experiment_id"], case_id=row["case_id"]
                )
                if row["quality"] is not None:
                    attach_quality_report(baseline, row["quality"], side="baseline")
                else:
                    # A missing candidate cannot certify independent baseline
                    # task quality. Keep the baseline run for unmatched pairing.
                    baseline.metadata.pop("quality_score", None)
                    baseline.metadata.pop(EVIDENCE_KEY, None)
                filename = "baseline-" + fingerprint(row["case_id"]) + ".json"
                write_once(root, folder + "/" + filename, baseline.to_dict())
                conditions["baseline"].append(filename)
                if row["trace"] is not None:
                    candidate = AgentTrace.from_dict(row["trace"])
                    candidate.metadata["agentloop.experiment_view"] = {
                        "source_trace_hash": fingerprint(row["trace"]),
                        "derived_fields": ["quality"],
                    }
                    attach_quality_report(candidate, row["quality"], side="candidate")
                    filename = "candidate-" + row["slot_id"] + ".json"
                    write_once(root, folder + "/" + filename, candidate.to_dict())
                    conditions["candidate"].append(filename)
            manifest = {
                "schema_version": "1.0",
                "name": report["name"] + " / " + name,
                "baseline": "baseline",
                "conditions": conditions,
                "pairing_keys": ["experiment_id", "case_id"],
            }
            path = write_once(root, folder + "/study.json", manifest)
            studies[name] = summarize_study(path)
        payload = {"experiment": report, "studies": studies}
        write_once(root, directory + "/evidence.json", payload)
        markdown = experiment_to_markdown(report)
        page = experiment_to_html(report)
        for name, text in (("evidence.md", markdown), ("evidence.html", page)):
            write_bytes_once(root, directory + "/" + name, text.encode("utf-8"))
        return {"directory": str(local_path(root, directory)), **payload}


def persist_experiment(root, store, *, project_id="default"):
    """Use the existing trace/finding/intervention store; no new database schema."""
    evidence = read_experiment(root)
    ids = []
    traces = [AgentTrace.from_dict(value) for value in evidence["baselines"].values()] + [
        AgentTrace.from_dict(row["trace"]) for row in evidence["rows"] if row["trace"] is not None
    ]
    existing = {}
    for trace in traces:
        saved = store.get_trace(trace.run_id, project_id=project_id)
        if saved is not None and fingerprint(saved.to_dict()) != fingerprint(trace.to_dict()):
            raise ValueError("native store already has conflicting evidence for a run identity")
        existing[trace.run_id] = saved is not None
    for case in evidence["specification"]["cases"]:
        trace = AgentTrace.from_dict(evidence["baselines"][case["case_id"]])
        if not existing[trace.run_id]:
            store.save_trace(trace, project_id=project_id)
            store.save_diagnosis(
                read_json(local_path(root, f"predictions/{fingerprint(case['case_id'])}.json")),
                project_id=project_id,
            )
    for row in evidence["rows"]:
        if row["trace"] is None:
            continue
        trace = AgentTrace.from_dict(row["trace"])
        if not existing[trace.run_id]:
            store.save_trace(trace, project_id=project_id)
        record = InterventionRecord.from_dict(row["intervention"])
        store.save_intervention(record, project_id=project_id)
        ids.append(record.intervention_id)
    return ids
