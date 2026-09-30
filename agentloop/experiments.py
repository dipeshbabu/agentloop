"""Bounded local candidate runs using native traces, budgets and interventions."""

from __future__ import annotations

import inspect
from fractions import Fraction
from pathlib import Path
from time import monotonic, perf_counter

from agentloop.ablation_protocol import timestamp
from agentloop.budget_types import DispatchOptions
from agentloop.budgets import BudgetLimits, budget_policy
from agentloop.events import utc_now_iso
from agentloop.experiment_artifacts import journal_lock, local_path, read_json, write_once
from agentloop.experiment_types import (
    EXPERIMENT_KEY,
    ExperimentCase,
    ExperimentPlan,
    ExperimentRequest,
    ExperimentResult,
    ExperimentRunner,
    canonical,
    fingerprint,
)
from agentloop.harness import Harness, HarnessConfig, HarnessControlError
from agentloop.interventions import build_intervention
from agentloop.quality import build_quality_report
from agentloop.replay import ReplayGates
from agentloop.tracer import AgentTrace, bind_trace_context
from agentloop.workflows import WorkflowInfo, record_operation, workflow_metadata


def slot_id(experiment_id, case_id, candidate_id):
    return fingerprint([experiment_id, case_id, candidate_id])


class ExperimentSession:
    """Resume finished slots idempotently; never repeat an uncertain attempt.

    The root is a local evidence journal, not a workflow engine. Callback I/O
    deadlines, provider limits and any side effects remain host responsibilities.
    """

    def __init__(self, plan, *, cases, runners, root):
        if (
            type(plan) is not ExperimentPlan
            or any(type(case) is not ExperimentCase for case in cases)
            or any(type(runner) is not ExperimentRunner for runner in runners)
        ):
            raise ValueError("typed experiment plan, cases and runner bindings required")
        self._plan, self._spec = plan, plan.to_dict()
        self._cases = {case.case_id: case for case in cases}
        self._runners = {runner.candidate_id: runner for runner in runners}
        if [case.declaration() for case in cases] != self._spec["cases"] or [
            runner.declaration() for runner in runners
        ] != self._spec["runners"]:
            raise ValueError("experiment bindings differ from the frozen plan")
        self.root = Path(root).resolve()
        with journal_lock(self.root):
            write_once(
                self.root,
                "experiment.json",
                {"experiment_id": plan.experiment_id, "specification": self._spec},
            )
            for case in cases:
                data = case.payload()
                write_once(
                    self.root, f"baselines/{fingerprint(case.case_id)}.json", data["baseline"]
                )
                write_once(
                    self.root, f"predictions/{fingerprint(case.case_id)}.json", data["diagnosis"]
                )

    @classmethod
    def resume(cls, root, *, cases, runners):
        saved = read_json(local_path(root, "experiment.json"))
        spec = saved["specification"]
        from agentloop.experiment_types import ExperimentBudget

        plan = ExperimentPlan(
            spec["name"],
            cases=cases,
            runners=runners,
            intervention_type=spec["intervention"]["type"],
            intervention_version=spec["intervention"]["version"],
            scorer=spec["scorer"],
            gate_version=spec["gate_version"],
            gates=ReplayGates(**spec["gates"]),
            budget=ExperimentBudget(**spec["budget"]),
            permission_ref=spec["permission_ref"],
            frozen_at=spec["frozen_at"],
            synthetic=spec["synthetic"],
        )
        if plan.experiment_id != saved["experiment_id"] or plan.to_dict() != spec:
            raise ValueError("saved experiment differs from supplied frozen bindings")
        return cls(plan, cases=cases, runners=runners, root=root)

    @property
    def experiment_id(self):
        return self._plan.experiment_id

    def _slots(self):
        for case in self._spec["cases"]:
            for runner in self._spec["runners"]:
                yield (
                    case,
                    runner,
                    slot_id(self.experiment_id, case["case_id"], runner["candidate_id"]),
                )

    def _ledger(self):
        from agentloop.experiment_reports import read_experiment

        if read_json(local_path(self.root, "experiment.json")) != {
            "experiment_id": self.experiment_id,
            "specification": self._spec,
        }:
            raise ValueError("frozen experiment declaration changed")
        evidence = read_experiment(self.root)
        if any(row["status"] == "unresolved" for row in evidence["rows"]):
            raise ValueError("uncertain experiment attempt requires host reconciliation")
        started = [row["admission"] for row in evidence["rows"] if row["admission"] is not None]
        receipts = [row["receipt"] for row in evidence["rows"] if row["receipt"] is not None]
        return started, receipts

    def run(self, *, enabled=False):
        if type(enabled) is not bool:
            raise ValueError("experiment execution requires explicit boolean opt-in")
        with journal_lock(self.root):
            if not enabled:
                return {"experiment_id": self.experiment_id, "enabled": False, "executed": 0}
            if timestamp(utc_now_iso()) < timestamp(self._spec["frozen_at"]):
                raise ValueError("experiment clock precedes its frozen declaration")
            started, receipts = self._ledger()
            finished = {receipt["slot_id"] for receipt in receipts}
            executed, stop_reason = 0, None
            budget = self._spec["budget"]
            consumed_tokens = sum(item["runner"]["reservation"]["tokens"] or 0 for item in started)
            consumed_cost = sum(
                (Fraction(str(item["runner"]["reservation"]["cost_usd"] or 0)) for item in started),
                Fraction(0),
            )
            halted = any(receipt["halted"] for receipt in receipts)
            for declared_case, declared_runner, identity in self._slots():
                if identity in finished:
                    continue
                if self._runners[declared_runner["candidate_id"]].declaration() != declared_runner:
                    raise ValueError("runner declaration changed before admission")
                if halted:
                    stop_reason = "prior_budget_control"
                    break
                reservation = declared_runner["reservation"]
                if (
                    len(started) >= budget["max_invocations"]
                    or (
                        budget["max_tokens"] is not None
                        and consumed_tokens + reservation["tokens"] > budget["max_tokens"]
                    )
                    or (
                        budget["max_cost_usd"] is not None
                        and consumed_cost + Fraction(str(reservation["cost_usd"]))
                        > Fraction(str(budget["max_cost_usd"]))
                    )
                ):
                    stop_reason = "execution_budget_exhausted"
                    break
                admission = {
                    "experiment_id": self.experiment_id,
                    "slot_id": identity,
                    "case_id": declared_case["case_id"],
                    "candidate_id": declared_runner["candidate_id"],
                    "runner": declared_runner,
                    "started_at": utc_now_iso(),
                }
                write_once(self.root, f"attempts/{identity}/started.json", admission)
                remaining = BudgetLimits(
                    max_tool_calls=budget["max_invocations"] - len(started),
                    max_tokens=None
                    if budget["max_tokens"] is None
                    else budget["max_tokens"] - consumed_tokens,
                    max_cost_usd=None
                    if budget["max_cost_usd"] is None
                    else float(Fraction(str(budget["max_cost_usd"])) - consumed_cost),
                )
                receipt, interruption = self._execute(
                    declared_case, declared_runner, admission, remaining
                )
                started.append(admission)
                receipts.append(receipt)
                consumed_tokens += reservation["tokens"] or 0
                consumed_cost += Fraction(str(reservation["cost_usd"] or 0))
                halted = receipt["halted"]
                executed += 1
                if interruption is not None:
                    raise interruption
            return {
                "experiment_id": self.experiment_id,
                "enabled": True,
                "executed": executed,
                "completed_slots": len(receipts),
                "stop_reason": stop_reason,
            }

    def _execute(self, declaration, runner_declaration, admission, remaining):
        case, runner = (
            self._cases[declaration["case_id"]],
            self._runners[runner_declaration["candidate_id"]],
        )
        data = case.payload()
        identity = admission["slot_id"]
        started = utc_now_iso()
        trace = AgentTrace(
            "Experiment candidate: " + runner.candidate_id,
            run_id="experiment_run_" + identity,
            started_at=started,
            metadata=workflow_metadata(
                WorkflowInfo(
                    self._spec["intervention"]["type"], self._spec["intervention"]["version"]
                ),
                task_id=case.example_id,
                example_id=case.example_id,
                status="running",
                metadata={
                    "synthetic": self._spec["synthetic"],
                    "experiment_id": self.experiment_id,
                    "case_id": case.case_id,
                    "repetition": case.repetition,
                    "condition": runner.candidate_id,
                },
            ),
        )
        run = Harness(
            HarnessConfig("enforce", (budget_policy(remaining, metered_boundaries=("tool",)),))
        ).start_run(trace.run_id)
        request = ExperimentRequest(
            self.experiment_id,
            case.case_id,
            runner.candidate_id,
            data["input_ref"],
            monotonic() + self._spec["budget"]["timeout_s"],
            canonical(data["inputs"]),
        )
        invoked, result, error, interruption = False, None, None, None
        status = "completed"
        root_event_id = "experiment_root_" + identity

        def invoke():
            nonlocal invoked, result
            if request.remaining_s <= 0:
                raise TimeoutError("candidate admission deadline expired")
            invoked = True
            with bind_trace_context(trace, root_event_id):
                value = runner.invoke(request)
            if type(value) is not ExperimentResult:
                if inspect.iscoroutine(value) or inspect.isgenerator(value):
                    value.close()
                raise ValueError("runner returned an invalid result")
            result = value
            return value

        began = perf_counter()
        with bind_trace_context(trace):
            try:
                run.wrap(
                    invoke,
                    boundary="tool",
                    branch_id="experiment",
                    dispatch=DispatchOptions(reservation=runner.reservation),
                    usage_reader=lambda value: value.usage,
                )()
                if runner.declaration() != runner_declaration:
                    raise ValueError("runner declaration changed")
            except HarnessControlError:
                status, error = "stopped", "budget_control"
            except TimeoutError:
                status, error = "timed_out", "runner_timeout"
            except Exception:
                status, error = "failed", "runner_error"
            except BaseException as exc:
                status, error, interruption = "cancelled", "runner_cancelled", exc
        elapsed = (perf_counter() - began) * 1000
        if status == "completed" and request.remaining_s <= 0:
            status, error = "timed_out", "runner_timeout"
        ended = utc_now_iso()
        trace.ended_at, trace.elapsed_ms = ended, elapsed
        workflow_status = (
            "completed"
            if status == "completed"
            else "cancelled"
            if status == "cancelled"
            else "failed"
        )
        trace.metadata["agentloop.workflow"]["status"] = workflow_status
        trace.metadata[EXPERIMENT_KEY] = {
            "schema_version": "1.0",
            "experiment_id": self.experiment_id,
            "slot_id": identity,
            "runner": runner_declaration,
            "status": status,
            "invoked": invoked,
        }
        if invoked:
            record_operation(
                "candidate runner",
                kind="workflow",
                trace=trace,
                event_id=root_event_id,
                started_at=started,
                ended_at=ended,
                duration_ms=elapsed,
                status="ok" if status == "completed" else "error",
                metadata={"error_type": error} if error else None,
            )
        try:
            baseline = AgentTrace.from_dict(data["baseline"])
            fixture = {
                "schema_version": "2.0",
                "id": case.case_id,
                "input_ref": data["input_ref"],
                "expected_ref": data["quality_ref"],
                "expected": data["expected"],
                "scorer": self._spec["scorer"],
                "baseline_output": data["baseline_output"],
                "candidate_status": "completed"
                if status == "completed"
                else "cancelled"
                if status == "cancelled"
                else "timed_out"
                if status == "timed_out"
                else "failed",
            }
            if status == "completed":
                fixture["candidate_output"] = result.output
            quality = build_quality_report(
                [fixture],
                baseline_trace=baseline,
                candidate_trace=trace,
                min_score=self._spec["gates"]["min_quality_score"],
            )
            trace.metadata.update(
                success=status == "completed" and quality["passed"],
                quality_score=quality["candidate_score"],
            )
            record = build_intervention(
                baseline,
                trace,
                target_finding_ids=declaration["source_finding_ids"],
                intervention_type=self._spec["intervention"]["type"],
                configuration={
                    "experiment_id": self.experiment_id,
                    "slot_id": identity,
                    "intervention_version": self._spec["intervention"]["version"],
                    "runner": runner_declaration,
                    "gate_version": self._spec["gate_version"],
                },
                diagnosis=data["diagnosis"],
                gates=ReplayGates(**self._spec["gates"]),
                quality_report=quality,
            )
            documents = {
                "trace.json": trace.to_dict(),
                "quality.json": quality,
                "intervention.json": record.to_dict(),
            }
            for filename, document in documents.items():
                write_once(self.root, f"attempts/{identity}/{filename}", document)
            receipt = {
                "schema_version": "1.0",
                "experiment_id": self.experiment_id,
                "slot_id": identity,
                "started_hash": fingerprint(admission),
                "case_id": case.case_id,
                "candidate_id": runner.candidate_id,
                "status": status,
                "error_category": error,
                "invoked": invoked,
                "halted": run.stopped,
                "budget_call_id": run.results[0].call_id,
                "budget_config_hash": run.harness.config.config_hash,
                "budget_dispatched": run.results[-1].dispatched,
                "usage": None if result is None else result.usage.to_dict(),
                "output_hash": None if result is None else fingerprint(result.output),
                "quality_passed": quality["passed"],
                "gates_passed": record.to_dict()["gates_passed"],
                "artifact_hashes": {name: fingerprint(value) for name, value in documents.items()},
            }
            write_once(self.root, f"attempts/{identity}/receipt.json", receipt)
            return receipt, interruption
        except BaseException:
            if interruption is not None:
                raise interruption
            raise
