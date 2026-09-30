"""Structural candidates built on the existing declared scheduler and experiments."""

from __future__ import annotations

from dataclasses import replace
from threading import RLock
from types import MappingProxyType

from agentloop.budget_types import ResourceUsage
from agentloop.context_experiments import _close_invalid, _deadline, _sum_usage
from agentloop.context_types import synchronous
from agentloop.experiment_observations import observation, record_experiment_observations
from agentloop.experiment_types import (
    ExperimentResult,
    ExperimentRunner,
    fingerprint,
    owned,
    reference,
)
from agentloop.harness import Harness, HarnessConfig
from agentloop.scheduling_plan import plan_tools
from agentloop.scheduling_types import ScheduleConfig, SchedulingError, ToolCall
from agentloop.structural_types import (
    STRUCTURAL_KEY,
    StageReference,
    publish_structural_observations,
)
from agentloop.tool_scheduling import ToolScheduler, scheduling_policy
from agentloop.tracer import current_trace
from agentloop.workflows import StageInfo, trace_operation


def _stages(values):
    if (
        not isinstance(values, (tuple, list))
        or not 1 <= len(values) <= 64
        or any(type(item) is not StageReference for item in values)
        or len({item.stage_id for item in values}) != len(values)
    ):
        raise ValueError("structural candidates require unique bounded stage references")
    return tuple(values)


def _graph_runner(
    candidate_id,
    factory,
    assemble,
    *,
    stages,
    factory_ref,
    assembler_ref,
    why_ref,
    removed,
    max_concurrency,
    strict,
    reservation,
):
    stages = _stages(stages)
    if not synchronous(factory) or not synchronous(assemble):
        raise ValueError("stage factory and assembler must be trusted synchronous callbacks")
    for name, value in (
        ("factory_ref", factory_ref),
        ("assembler_ref", assembler_ref),
        ("why_ref", why_ref),
    ):
        reference(value, name)
    removed = owned(removed)
    ids = {stage.stage_id for stage in stages}
    if not isinstance(removed, dict) or not set(removed) <= ids:
        raise ValueError("removed stages must belong to the explicit stage set")
    settings = ScheduleConfig(
        "structural-schedule",
        "1.0",
        "stages",
        max_concurrency=max_concurrency,
        on_unknown="error" if strict else "sequential",
    )
    config = {
        "schema_version": "1.0",
        "family": "parallelization" if strict else "stage_removal",
        "stages": [stage.declaration() for stage in stages],
        "factory_ref": factory_ref,
        "assembler_ref": assembler_ref,
        "why_ref": why_ref,
        "removed": {key: fingerprint(value) for key, value in removed.items()},
        "scheduler": settings.to_dict(),
    }
    config_hash = fingerprint(config)
    source = "structural-config:" + config_hash

    def invoke(request):
        trace = current_trace()
        if trace is None:
            raise ValueError("structural candidate requires an active experiment")
        receipt = {
            "schema_version": "1.0",
            "config_hash": config_hash,
            "family": config["family"],
            "why_ref": why_ref,
            "changed_stages": sorted(ids if strict else removed),
            "stages": [stage.declaration() for stage in stages],
            "removed_value_hashes": config["removed"],
            "state": "preparing",
            "invoked_stages": [],
            "skipped_stages": [],
            "output_hashes": {},
            "usage": {},
            "scheduler": None,
        }
        trace.metadata[STRUCTURAL_KEY] = receipt
        lock, event_ids = RLock(), {}
        scheduler = ToolScheduler(
            Harness(HarnessConfig("enforce", (scheduling_policy(settings),))).start_run(),
            config=settings,
        )
        result = None
        try:
            _deadline(request)
            calls = factory(request)
            if (
                not isinstance(calls, (tuple, list))
                or any(type(call) is not ToolCall for call in calls)
                or {call.call_id for call in calls} != ids
                or len(calls) != len(ids)
            ):
                raise ValueError("stage factory differs from declared stage identities")
            # Validate the original dependency/resource contract before any
            # removal can hide an unsafe or cyclic source plan.
            receipt["original_plan"] = plan_tools(calls, settings)
            references = {stage.stage_id: stage for stage in stages}
            wrapped = []
            for call in calls:
                if call.call_id in removed:

                    def skipped(context, call=call):
                        with lock:
                            receipt["skipped_stages"].append(call.call_id)
                        return ExperimentResult(
                            removed[call.call_id],
                            # Fixed accounting provenance, not a credential.
                            usage=ResourceUsage(
                                tokens=0,
                                token_provenance="user_supplied",
                                complete=True,  # nosec B106
                            ),
                        )

                    wrapped.append(
                        replace(
                            call,
                            invoke=skipped,
                            effect="read_only",
                            reads=(),
                            writes=(),
                            concurrent=True,
                            usage_reader=lambda value: value.usage,
                            error_usage_reader=None,
                        )
                    )
                    continue

                def measured(context, call=call):
                    _deadline(request)
                    stage = references[call.call_id]
                    with lock:
                        dependencies = [
                            event_ids[key] for key in (call.depends_on or ()) if key in event_ids
                        ]
                        receipt["invoked_stages"].append(call.call_id)
                    copied = replace(
                        context,
                        prerequisites=MappingProxyType(
                            {key: value.output for key, value in context.prerequisites.items()}
                        ),
                    )
                    with trace_operation(
                        call.call_id,
                        kind=stage.kind,
                        stage=StageInfo(
                            call.call_id,
                            stage.version,
                            kind=stage.kind,
                            input_ref=request.input_ref,
                        ),
                        depends_on=dependencies if call.depends_on is not None else None,
                        metadata={
                            "structural_source_ref": stage.source_ref,
                            "parallel_safe": call.concurrent is True and call.effect == "read_only",
                        },
                    ) as span:
                        with lock:
                            event_ids[call.call_id] = span.event_id
                        value = call.invoke(copied)
                        if type(value) is not ExperimentResult:
                            _close_invalid(value)
                            raise ValueError("stage callback must return ExperimentResult")
                        with lock:
                            receipt["output_hashes"][call.call_id] = fingerprint(value.output)
                            receipt["usage"][call.call_id] = value.usage.to_dict()
                        return value

                wrapped.append(
                    replace(
                        call,
                        invoke=measured,
                        usage_reader=lambda value: value.usage,
                        error_usage_reader=None,
                    )
                )
            result = scheduler.execute(wrapped, timeout_s=request.remaining_s)
            if not result.completed:
                raise ValueError("structural stage execution is incomplete")
            _deadline(request)
            outputs = MappingProxyType(
                {item.call_id: item.value.output for item in result.outcomes}
            )
            value = assemble(request, outputs)
            if type(value) is not ExperimentResult:
                _close_invalid(value)
                raise ValueError("assembler must return ExperimentResult with usage")
            receipt["assembler_usage"] = value.usage.to_dict()
            usages = [item.value.usage for item in result.outcomes] + [value.usage]
            receipt["state"] = "completed"
            return ExperimentResult(value.output, usage=_sum_usage(usages))
        except SchedulingError as exc:
            receipt.update(state="abstained", reason=exc.reason_code)
            raise
        except BaseException:
            receipt["state"] = "failed"
            raise
        finally:
            receipt["scheduler"] = scheduler.export_evidence()
            receipt["invoked_stages"].sort()
            receipt["skipped_stages"].sort()
            evidence = list(receipt["scheduler"]["records"].values())
            peak = evidence[0]["peak_running"] if evidence else 0
            observed = result or scheduler.last_result
            failures = (
                sum(item.status != "completed" for item in observed.outcomes)
                if observed is not None
                else None
            )
            publish_structural_observations(
                receipt,
                {
                    "structural_state": observation(
                        receipt["state"], unit="state", source_ref=source
                    ),
                    "stages_invoked": observation(
                        len(receipt["invoked_stages"]), unit="operations", source_ref=source
                    ),
                    "stages_skipped": observation(
                        len(receipt["skipped_stages"]), unit="operations", source_ref=source
                    ),
                    "stage_failures": observation(failures, unit="operations", source_ref=source),
                    "peak_parallel_stages": observation(peak, unit="operations", source_ref=source),
                    "parallel_execution_observed": observation(
                        peak > 1, unit="boolean", source_ref=source
                    ),
                },
            )

    return ExperimentRunner(
        candidate_id,
        "1.0",
        "agentloop.structural-" + config["family"] + ":1.0",
        "sha256:" + config_hash,
        invoke,
        reservation=reservation,
    )


def stage_removal_runner(
    candidate_id,
    factory,
    assemble,
    *,
    stages,
    removed,
    factory_ref,
    assembler_ref,
    why_ref,
    reservation,
):
    if not removed:
        raise ValueError(
            "stage removal requires explicit replacement values for omitted operations"
        )
    return _graph_runner(
        candidate_id,
        factory,
        assemble,
        stages=stages,
        factory_ref=factory_ref,
        assembler_ref=assembler_ref,
        why_ref=why_ref,
        removed=removed,
        max_concurrency=1,
        strict=False,
        reservation=reservation,
    )


def parallel_experiment_runner(
    candidate_id,
    factory,
    assemble,
    *,
    stages,
    factory_ref,
    assembler_ref,
    why_ref,
    reservation,
    max_concurrency=4,
):
    return _graph_runner(
        candidate_id,
        factory,
        assemble,
        stages=stages,
        factory_ref=factory_ref,
        assembler_ref=assembler_ref,
        why_ref=why_ref,
        removed={},
        max_concurrency=max_concurrency,
        strict=True,
        reservation=reservation,
    )


def conditional_experiment_runner(
    candidate_id,
    expensive,
    fallback,
    predicate,
    *,
    stages,
    predicate_ref,
    why_ref,
    reservation,
    on_unknown="expensive",
):
    stages = _stages(stages)
    if (
        type(expensive) is not ExperimentRunner
        or type(fallback) is not ExperimentRunner
        or not synchronous(predicate)
        or on_unknown not in {"expensive", "fallback", "error"}
    ):
        raise ValueError(
            "conditional candidates require explicit implementations and unknown handling"
        )
    reference(predicate_ref, "predicate_ref")
    reference(why_ref, "why_ref")
    config = {
        "schema_version": "1.0",
        "stages": [stage.declaration() for stage in stages],
        "expensive": expensive.declaration(),
        "fallback": fallback.declaration(),
        "predicate_ref": predicate_ref,
        "why_ref": why_ref,
        "on_unknown": on_unknown,
    }
    config_hash = fingerprint(config)
    source = "conditional-config:" + config_hash

    def invoke(request):
        if (
            expensive.declaration() != config["expensive"]
            or fallback.declaration() != config["fallback"]
        ):
            raise ValueError("conditional implementation binding changed")
        trace = current_trace()
        receipt = {
            "schema_version": "1.0",
            "family": "conditional",
            "config_hash": config_hash,
            "why_ref": why_ref,
            "changed_stages": [stage.declaration() for stage in stages],
            "predicate": None,
            "route": None,
            "unknown_reason": None,
        }
        trace.metadata[STRUCTURAL_KEY] = receipt
        _deadline(request)
        usage = ResourceUsage(tokens=0, token_provenance="user_supplied", complete=True)  # nosec B106
        try:
            value = predicate(request)
            reported_usage = type(value) is ExperimentResult
            if type(value) is ExperimentResult:
                usage, value = value.usage, value.output
            if value is not None and type(value) is not bool:
                _close_invalid(value)
                if not reported_usage:
                    usage = ResourceUsage()
                value, receipt["unknown_reason"] = None, "invalid_predicate"
        except Exception:
            value, usage, receipt["unknown_reason"] = None, ResourceUsage(), "predicate_error"
        known = type(value) is bool
        route = ("expensive" if value else "fallback") if known else on_unknown
        receipt.update(predicate=value, route=route, predicate_usage=usage.to_dict())
        record_experiment_observations(
            {
                "conditional_predicate_known": observation(
                    known, unit="boolean", source_ref=source
                ),
                "conditional_predicate_value": observation(
                    value, unit="boolean", source_ref=source
                ),
                "conditional_route": observation(route, unit="state", source_ref=source),
                "conditional_unknown": observation(not known, unit="boolean", source_ref=source),
            }
        )
        if route == "error":
            raise ValueError("conditional predicate is unknown")
        _deadline(request)
        result = (expensive if route == "expensive" else fallback).invoke(request)
        if type(result) is not ExperimentResult:
            _close_invalid(result)
            raise ValueError("conditional implementation must return ExperimentResult")
        receipt["selected_usage"] = result.usage.to_dict()
        return ExperimentResult(result.output, usage=_sum_usage([usage, result.usage]))

    return ExperimentRunner(
        candidate_id,
        "1.0",
        "agentloop.conditional-execution:1.0",
        "sha256:" + config_hash,
        invoke,
        reservation=reservation,
    )
