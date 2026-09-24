"""Inventory original real-study predictions without rewriting their attribution."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentloop.calibration import calibration_to_markdown, summarize_calibration
from examples.real_agent_study.run import fingerprint, write_new


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def archive_time(path, *, before=None):
    """Use an actual original file's write time, never a fabricated earlier time."""
    value = datetime.fromtimestamp(Path(path).stat().st_mtime, timezone.utc)
    if before is not None and value >= datetime.fromisoformat(before):
        return None
    return value.isoformat()


def context(workload, models, environment, scorer, intervention):
    return {
        "workload": workload,
        "model": "sha256:" + fingerprint(models),
        "provider": "llama.cpp:b10964:b29c606e2:Vulkan",
        "environment": "sha256:" + fingerprint(environment),
        "scorer": scorer,
        "policy": None,
        "cost_basis": "unknown",
        "pricing": None,
        "quality_gate": {"min_score": 1, "max_regression": 0},
        "intervention": intervention,
    }


class Inventory:
    def __init__(self, out):
        self.out = Path(out)
        if self.out.exists() and any(self.out.iterdir()):
            raise ValueError("calibration output must be fresh or empty")
        self.out.mkdir(parents=True, exist_ok=True)
        self.registrations = []
        self.provenance = []
        self.copied = {}

    def copy(self, source, relative):
        source = Path(source).resolve()
        if source in self.copied:
            return self.copied[source]
        target = (self.out / relative).resolve()
        if not target.is_relative_to(self.out.resolve()):
            raise ValueError("source artifact path escaped inventory")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(source.read_bytes())
        value = target.relative_to(self.out.resolve()).as_posix()
        self.copied[source] = value
        return value

    def routing_study(self, root):
        root = Path(root)
        environment = {
            "agentloop": "0.7.0",
            "langgraph": "1.2.11",
            "python": "3.13.12",
            "gpu": "GTX1650:4GB",
            "driver": "566.36",
            "provider": "llama.cpp:b10964:Vulkan",
        }
        for phase, protocol_name in (
            ("pilot", "pilot-frozen.json"),
            ("evaluation", "evaluation-frozen.json"),
        ):
            protocol = read(root / protocol_name)
            protocol_hash = fingerprint(protocol)
            tasks = [task for task in protocol["tasks"] if task["split"] == phase]
            records = {}
            for path in (root / f"{phase}-report/interventions").glob("*.json"):
                value = read(path)
                key = (value["metadata"]["task_id"], value["metadata"]["repetition"])
                if key in records:
                    raise ValueError("ambiguous original intervention")
                records[key] = (path, value)
            for task in tasks:
                repetitions = range(protocol["repetitions"]) if phase == "evaluation" else range(1)
                for repetition in repetitions:
                    before = (
                        root / "runs" / phase / "baseline" / f"rep-{repetition}-traced" / task["id"]
                    ).resolve()
                    after = (
                        root
                        / "runs"
                        / phase
                        / "candidate"
                        / f"rep-{repetition}-traced"
                        / task["id"]
                    ).resolve()
                    if not before.is_relative_to(root.resolve()) or not after.is_relative_to(
                        root.resolve()
                    ):
                        raise ValueError("invalid task path")
                    original, candidate = (
                        read(before / "receipt.json"),
                        read(after / "receipt.json"),
                    )
                    diagnosis = read(before / "diagnosis.json")
                    if original["protocol_hash"] != protocol_hash or original[
                        "diagnosis_hash"
                    ] != fingerprint(diagnosis):
                        raise ValueError("original archived diagnosis changed")
                    record_path, record = records[(task["id"], repetition)]
                    selected = set(record["target_finding_ids"])
                    before_ref = self.copy(
                        before / "trace.json",
                        f"routing/{phase}/{task['id']}/{repetition}/baseline.json",
                    )
                    after_ref = self.copy(
                        after / "trace.json",
                        f"routing/{phase}/{task['id']}/{repetition}/candidate.json",
                    )
                    record_ref = self.copy(
                        record_path, f"routing/{phase}/{task['id']}/{repetition}/intervention.json"
                    )
                    candidate_trace = read(after / "trace.json")
                    prediction_time = archive_time(
                        before / "diagnosis.json", before=candidate_trace["started_at"]
                    )
                    outcome_time = archive_time(record_path)
                    declaration = context(
                        "agent-tool-loop:"
                        + task["workload"]
                        + ":"
                        + fingerprint(protocol["implementation_hashes"]),
                        protocol["models"],
                        environment,
                        protocol["scorer_version"]
                        + ":"
                        + protocol["implementation_hashes"]["scoring.py"],
                        {
                            "type": record["intervention_type"],
                            "configuration": record["configuration"],
                        },
                    )
                    for index, finding in enumerate(diagnosis["findings"]):
                        chosen = finding["finding_id"] in selected
                        case_id = f"routing-{phase}-{task['id']}-{repetition}-{index}"
                        self.registrations.append(
                            {
                                "case_id": case_id,
                                "task_id": task["id"],
                                "task_sha256": fingerprint(task),
                                "repetition": repetition,
                                "split": "fit" if phase == "pilot" else "held_out",
                                "selection": "selected" if chosen else "rejected",
                                "selection_reason": "preregistered_routing"
                                if chosen
                                else "outside_selected_routing_scope",
                                "synthetic": False,
                                "context": declaration,
                                "prediction": finding,
                                "prediction_recorded_at": prediction_time,
                                "outcome": {
                                    "record": record_ref,
                                    "baseline_trace": before_ref,
                                    "candidate_trace": after_ref,
                                    "recorded_at": outcome_time,
                                    "status": "completed"
                                    if candidate["status"] == "completed"
                                    else "failed",
                                }
                                if chosen
                                else None,
                            }
                        )
                        self.provenance.append(
                            {
                                "case_id": case_id,
                                "prediction_hash": fingerprint(finding),
                                "diagnosis_hash": original["diagnosis_hash"],
                                "timestamp_basis": "original archived file last-write time, bound to already-published content hash; not independent preregistration proof"
                                if prediction_time
                                else "archive time unavailable after relocation or rewrite",
                                "combined_finding_count": len(record["predicted"]["findings"])
                                if chosen
                                else None,
                            }
                        )

    def single_decision_study(self, root):
        root = Path(root)
        protocol = read(root / "protocol.json")
        declaration = context(
            protocol["workload"] + ":" + fingerprint(protocol["configuration"]),
            protocol["models"],
            protocol["environment"],
            protocol["scorer_version"] + ":" + protocol["code_hashes"]["runner"],
            protocol["intervention"],
        )
        for task in protocol["tasks"]:
            for repetition in range(protocol["configuration"]["repetitions"]):
                before = (root / "baseline" / str(repetition) / task["id"]).resolve()
                after = (root / "candidate" / str(repetition) / task["id"]).resolve()
                if not before.is_relative_to(root.resolve()) or not after.is_relative_to(
                    root.resolve()
                ):
                    raise ValueError("invalid task path")
                prediction = read(before / "prediction.json")
                prediction_journal = read(before / "journal.json")
                if prediction_journal["prediction_hash"] != fingerprint(
                    prediction
                ) or prediction_journal["protocol_hash"] != fingerprint(protocol):
                    raise ValueError("original single-decision prediction changed")
                outcome = None
                if (after / "intervention.json").exists():
                    record, journal = (
                        read(after / "intervention.json"),
                        read(after / "journal.json"),
                    )
                    if journal["record_hash"] != fingerprint(record):
                        raise ValueError("original single-decision ledger changed")
                    receipt = read(after / "receipt.json")
                    prefix = f"single/{task['id']}/{repetition}"
                    outcome = {
                        "record": self.copy(
                            after / "intervention.json", prefix + "/intervention.json"
                        ),
                        "baseline_trace": self.copy(
                            before / "trace.json", prefix + "/baseline.json"
                        ),
                        "candidate_trace": self.copy(
                            after / "trace.json", prefix + "/candidate.json"
                        ),
                        "recorded_at": journal["outcome_recorded_at"],
                        "status": receipt["status"],
                    }
                case_id = f"single-{task['id']}-{repetition}"
                self.registrations.append(
                    {
                        "case_id": case_id,
                        "task_id": task["id"],
                        "task_sha256": fingerprint(task),
                        "repetition": repetition,
                        "split": task["split"],
                        "selection": "selected",
                        "selection_reason": "frozen_single_decision_routing",
                        "synthetic": False,
                        "context": declaration,
                        "prediction": prediction,
                        "prediction_recorded_at": prediction_journal["prediction_recorded_at"],
                        "outcome": outcome,
                    }
                )
                self.provenance.append(
                    {
                        "case_id": case_id,
                        "prediction_hash": fingerprint(prediction),
                        "timestamp_basis": "explicit post-write prediction journal before candidate; post-write immutable outcome journal",
                        "combined_finding_count": 1,
                    }
                )

    def finish(self):
        now = datetime.now(timezone.utc)
        manifest = {
            "schema_version": "1.0",
            "name": "Real local-model historical calibration: attribution retained",
            "as_of": now.isoformat(),
            "valid_until": (now + timedelta(days=7)).isoformat(),
            "fit_method": "scale",
            "min_fit_tasks": 2,
            "selection_inventory_complete": True,
            "bootstrap": {"samples": 1000, "seed": 20260923, "confidence": 0.95},
            "registrations": self.registrations,
        }
        write_new(self.out / "manifest.json", manifest)
        write_new(
            self.out / "provenance.json",
            {
                "registrations": self.provenance,
                "scope": "Complete finding inventory for the revised #181 pilot/evaluation and the separate single-decision experiment. The retired onboarding protocol is retained in #181's bundle and outside this cohort inventory. #188 count caps have no recommendation-estimator attribution and are not fitted. No original ledger, prediction, or runtime coefficient is changed.",
            },
        )
        report = summarize_calibration(self.out / "manifest.json")
        write_new(self.out / "report.json", report)
        (self.out / "report.md").write_text(calibration_to_markdown(report), encoding="utf-8")
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routing-study", type=Path, required=True)
    parser.add_argument("--single-study", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    inventory = Inventory(args.out)
    inventory.routing_study(args.routing_study)
    inventory.single_decision_study(args.single_study)
    result = inventory.finish()
    print(
        json.dumps(
            {
                "registrations": result["registration_count"],
                "cohorts": len(result["cohorts"]),
                "synthetic_excluded": result["synthetic_excluded_count"],
            }
        )
    )
