"""Validate retained host evidence and reuse native ablation/study exporters."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "real_harness_study"

from agentloop.ablations import ablation_to_markdown, build_ablation_report, summarize_ablation
from agentloop.html_report import analysis_to_html
from agentloop.tracer import AgentTrace

from .protocol import load_plan

try:
    from real_agent_study.run import fingerprint, write_new
    from real_agent_study.scoring import grade
except ModuleNotFoundError:
    from examples.real_agent_study.run import fingerprint, write_new
    from examples.real_agent_study.scoring import grade


def export(plan_path, out):
    plan_path, out = Path(plan_path), Path(out)
    plan = load_plan(plan_path)
    root = plan_path.parent
    if out.exists() and any(out.iterdir()):
        raise ValueError("export directory must be fresh or empty")
    out.mkdir(parents=True, exist_ok=True)
    spec = plan["protocol"]["specification"]
    tasks = {task["id"]: task for task in plan["tasks"]}
    observations, sources, unavailable, seen = [], {}, [], set()
    manifests = {
        name: []
        for name, condition in spec["conditions"].items()
        if condition["mode"] != "uninstrumented"
    }
    html_done = set()
    expected_folders = {
        "obs_"
        + fingerprint(
            [plan["protocol"]["protocol_hash"], slot["task_id"], slot["repetition"], condition]
        )[:24]
        for slot in spec["schedule"]
        for condition in slot["order"]
    }
    if (root / "attempts").exists() and any(
        path.name not in expected_folders for path in (root / "attempts").iterdir() if path.is_dir()
    ):
        raise ValueError(
            "unplanned attempt directory; retain and review it explicitly instead of dropping it"
        )
    for slot in spec["schedule"]:
        for position, condition in enumerate(slot["order"]):
            identity = (
                "obs_"
                + fingerprint(
                    [
                        plan["protocol"]["protocol_hash"],
                        slot["task_id"],
                        slot["repetition"],
                        condition,
                    ]
                )[:24]
            )
            folder = root / "attempts" / identity
            path = folder / "receipt.json"
            if not path.exists():
                unavailable.append(
                    {
                        "observation_id": identity,
                        "status": "incomplete"
                        if (folder / "started.json").exists()
                        else "not_recorded",
                    }
                )
                continue
            receipt = json.loads(path.read_text(encoding="utf-8"))
            row = receipt["observation"]
            expected = {
                "observation_id": identity,
                "protocol_hash": plan["protocol"]["protocol_hash"],
                "task_id": slot["task_id"],
                "repetition": slot["repetition"],
                "cache_condition": slot["cache_condition"],
                "condition": condition,
                "position": position,
                "versions": spec["versions"],
            }
            if any(row.get(key) != value for key, value in expected.items()) or identity in seen:
                raise ValueError("observation does not match its frozen slot")
            seen.add(identity)
            original = json.loads((folder / "observation.json").read_text(encoding="utf-8"))
            if original != row or receipt["quality"]["score"] != row["quality_score"]:
                raise ValueError("observation/receipt mismatch")
            if row["status"] == "completed":
                quality = grade(
                    receipt["output"]["answer"],
                    tasks[row["task_id"]],
                    sources={},
                    tool_history=receipt["output"]["tool_history"],
                )
                if quality["score"] != row["quality_score"] or quality["passed"] != row["success"]:
                    raise ValueError("independent quality cannot be reproduced")
            elif row["success"] or row["quality_score"] not in {None, 0}:
                raise ValueError("unfinished work cannot pass quality")
            if row["trace_run_id"] is not None:
                trace = AgentTrace.from_json(folder / "trace.json")
                diagnosis = json.loads((folder / "diagnosis.json").read_text(encoding="utf-8"))
                if (
                    fingerprint(trace.to_dict()) != receipt["trace_hash"]
                    or fingerprint(diagnosis) != receipt["diagnosis_hash"]
                    or trace.run_id != row["trace_run_id"]
                ):
                    raise ValueError("trace or archived diagnosis changed")
                if trace.metadata.get("agentloop.harness") != receipt["policy_evidence"]:
                    raise ValueError("native policy evidence diverged from the host receipt")
                target = out / "traces" / condition / f"{identity}.json"
                trace.export_json(target)
                manifests[condition].append(target.relative_to(out).as_posix())
                if condition not in html_done:
                    payload = {
                        "trace": trace.to_dict(),
                        "report": trace.report(),
                        "diagnosis": diagnosis,
                        "optimization": json.loads(
                            (folder / "plan.json").read_text(encoding="utf-8")
                        ),
                    }
                    (out / f"{condition}.html").write_text(
                        analysis_to_html(payload), encoding="utf-8"
                    )
                    html_done.add(condition)
            elif (folder / "trace.json").exists() or receipt["policy_evidence"] is not None:
                raise ValueError("uninstrumented arm must not fabricate traces or policy evidence")
            observations.append(row)
            sources[identity] = fingerprint(receipt)
    # The native contract permits an empty ledger list. A count cap is an
    # explicitly chosen safety policy, not implementation of a pricing/cache/
    # batching recommendation. Do not invent attribution to a different finding.
    manifests = {name: paths for name, paths in manifests.items() if paths}
    if len(manifests) < 2 or "trace" not in manifests:
        report = build_ablation_report(plan["protocol"], observations)
        report["artifact_links_complete"] = False
        report["artifact_link_reason"] = (
            "A linked native study requires a recorded tracing baseline and at least two traced conditions; all original observations and available trace files remain retained."
        )
        write_new(out / "observations.json", observations)
        write_new(out / "report.json", report)
        (out / "report.md").write_text(
            ablation_to_markdown(report) + "\n" + report["artifact_link_reason"] + "\n",
            encoding="utf-8",
        )
        write_new(out / "provenance.json", {"receipt_hashes": sources, "unavailable": unavailable})
        return report
    manifest = {
        "schema_version": "1.0",
        "name": spec["name"],
        "baseline": "trace",
        "conditions": manifests,
        "pairing_keys": ["task_id", "repetition", "cache_condition"],
        "bootstrap": spec["bootstrap"],
    }
    write_new(out / "study.json", manifest)
    bundle = {
        "schema_version": "1.0",
        "protocol": plan["protocol"],
        "observations": observations,
        "study_manifest": "study.json",
        "interventions": [],
    }
    write_new(out / "bundle.json", bundle)
    report = summarize_ablation(out / "bundle.json")
    write_new(out / "report.json", report)
    (out / "report.md").write_text(ablation_to_markdown(report), encoding="utf-8")
    write_new(
        out / "provenance.json",
        {
            "receipt_hashes": sources,
            "unavailable": unavailable,
            "ledger_scope": "No matching recommendation-based intervention; count caps are explicit safety policies. Native study/ablation and decision evidence are retained without invented estimator attribution.",
        },
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = export(args.plan, args.out)
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "planned_observation_count",
                    "observed_count",
                    "status_counts",
                    "stop_reason_counts",
                )
            }
        )
    )
