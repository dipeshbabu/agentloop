"""Bounded caller-provided batch candidates with retained per-item outcomes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from time import perf_counter

from agentloop.budget_types import Reservation
from agentloop.context_experiments import _close_invalid, _deadline, _path, _sum_usage, _value
from agentloop.context_types import EXACT_COUNTS, ContextTokenCount, identifier, synchronous
from agentloop.experiment_observations import observation
from agentloop.experiment_types import (
    ExperimentResult,
    ExperimentRunner,
    canonical,
    fingerprint,
    reference,
)
from agentloop.structural_types import (
    STRUCTURAL_KEY,
    BatchItem,
    BatchRequest,
    BatchResult,
    StageReference,
    publish_structural_observations,
)
from agentloop.studies import summarize_values
from agentloop.tracer import current_trace
from agentloop.workflows import StageInfo, trace_operation


@dataclass(frozen=True)
class BatchConstraints:
    batch_size: int
    provider_max_items: int
    provider_max_payload_bytes: int
    provider_ref: str
    max_items: int = 10000
    max_batches: int = 128
    provider_max_input_tokens: int | None = None

    def __post_init__(self):
        for name in (
            "batch_size",
            "provider_max_items",
            "provider_max_payload_bytes",
            "max_items",
            "max_batches",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError("batch bounds must be positive integers")
        if (
            self.batch_size > self.provider_max_items
            or self.provider_max_items > 1024
            or self.max_items > 10000
            or self.max_batches > 1000
        ):
            raise ValueError("batch configuration exceeds declared bounds")
        if self.provider_max_input_tokens is not None and (
            type(self.provider_max_input_tokens) is not int or self.provider_max_input_tokens < 1
        ):
            raise ValueError("input token limit must be a positive integer")
        reference(self.provider_ref, "provider constraint reference")


def batching_experiment_runner(
    candidate_id,
    invoke_batch,
    *,
    stage,
    items_path,
    constraints,
    implementation_ref,
    configuration_ref,
    why_ref,
    reservation=None,
    token_counter=None,
    counter_ref=None,
    on_item_failure="fail_task",
):
    if (
        type(stage) is not StageReference
        or type(constraints) is not BatchConstraints
        or not synchronous(invoke_batch)
    ):
        raise ValueError(
            "batch experiment requires a stage, constraints and trusted batch implementation"
        )
    path = _path(items_path)
    for name, value in (
        ("implementation_ref", implementation_ref),
        ("configuration_ref", configuration_ref),
        ("why_ref", why_ref),
    ):
        reference(value, name)
    if constraints.provider_max_input_tokens is not None and (
        not synchronous(token_counter) or counter_ref is None
    ):
        raise ValueError("token-constrained batches require a declared exact counter")
    if counter_ref is not None:
        identifier(counter_ref, "counter_ref")
    if on_item_failure not in {"fail_task", "return_partial"}:
        raise ValueError("unknown batch failure semantics")
    config = {
        "schema_version": "1.0",
        "stage": stage.declaration(),
        "items_path": path,
        "constraints": asdict(constraints),
        "implementation_ref": implementation_ref,
        "configuration_ref": configuration_ref,
        "why_ref": why_ref,
        "counter_ref": counter_ref,
        "on_item_failure": on_item_failure,
    }
    config_hash = fingerprint(config)
    source = "batch-config:" + config_hash

    def invoke(request):
        trace = current_trace()
        if trace is None:
            raise ValueError("batch candidate requires an active experiment")
        receipt = {
            "schema_version": "1.0",
            "family": "batching",
            "config_hash": config_hash,
            "stage": stage.declaration(),
            "why_ref": why_ref,
            "constraints": asdict(constraints),
            "state": "preparing",
            "batches": [],
            "items_planned": None,
        }
        trace.metadata[STRUCTURAL_KEY] = receipt
        completed, failed, unknown, invoked, latencies, usages, outputs = 0, 0, 0, 0, [], [], []
        items = None
        try:
            items = _value(request.inputs, path)
            if not isinstance(items, list) or len(items) > constraints.max_items:
                raise ValueError("batch input must be a bounded item list")
            receipt["items_planned"] = len(items)
            planned_batches = (len(items) + constraints.batch_size - 1) // constraints.batch_size
            if planned_batches > constraints.max_batches:
                raise ValueError("planned batches exceed the execution bound")
            for offset in range(0, len(items), constraints.batch_size):
                batch = tuple(
                    BatchItem(f"item_{index:06d}", items[index])
                    for index in range(offset, min(offset + constraints.batch_size, len(items)))
                )
                batch_id = f"batch_{offset // constraints.batch_size:06d}"
                record = {
                    "batch_id": batch_id,
                    "item_ids": [item.item_id for item in batch],
                    "input_hashes": [fingerprint(item.inputs) for item in batch],
                    "status": "pending",
                    "latency_ms": None,
                    "outcomes": [],
                    "usage": None,
                }
                receipt["batches"].append(record)
                encoded = canonical(
                    [{"item_id": item.item_id, "inputs": item.inputs} for item in batch]
                )
                if len(encoded.encode("utf-8")) > constraints.provider_max_payload_bytes:
                    record["status"] = "provider_constraint_rejected"
                    raise ValueError("batch exceeds declared provider payload bound")
                _deadline(request)
                if constraints.provider_max_input_tokens is not None:
                    count = token_counter(batch)
                    if (
                        type(count) is not ContextTokenCount
                        or count.provenance not in EXACT_COUNTS
                        or count.reference != counter_ref
                        or count.value is None
                        or count.value > constraints.provider_max_input_tokens
                    ):
                        record["status"] = "provider_constraint_rejected"
                        _close_invalid(count)
                        raise ValueError("batch input token bound is unavailable or exceeded")
                    record["input_token_count"] = count.to_dict()
                _deadline(request)
                began = perf_counter()
                invoked += 1
                try:
                    with trace_operation(
                        stage.stage_id,
                        kind=stage.kind,
                        stage=StageInfo(
                            stage.stage_id,
                            stage.version,
                            kind=stage.kind,
                            input_ref=request.input_ref,
                        ),
                        metadata={
                            "batch_id": batch_id,
                            "batch_size": len(batch),
                            "constraint_ref": constraints.provider_ref,
                        },
                    ):
                        result = invoke_batch(BatchRequest(batch_id, batch, request))
                    if type(result) is not BatchResult:
                        _close_invalid(result)
                        raise ValueError("batch implementation must return BatchResult")
                    record["usage"] = result.usage.to_dict()
                    usages.append(result.usage)
                    expected = {item.item_id for item in batch}
                    if (
                        len(result.items) != len(expected)
                        or {item.item_id for item in result.items} != expected
                    ):
                        raise ValueError(
                            "batch outcomes must match every planned item exactly once"
                        )
                    by_id = {item.item_id: item for item in result.items}
                    for item in batch:
                        outcome = by_id[item.item_id]
                        record["outcomes"].append(
                            {
                                "item_id": item.item_id,
                                "status": outcome.status,
                                "error_code": outcome.error_code,
                                "output_hash": fingerprint(outcome.output)
                                if outcome.status == "completed"
                                else None,
                            }
                        )
                        outputs.append(outcome.output if outcome.status == "completed" else None)
                        completed += outcome.status == "completed"
                        failed += outcome.status == "failed"
                        unknown += outcome.status == "unknown"
                    record["status"] = (
                        "completed"
                        if all(item.status == "completed" for item in result.items)
                        else "partial"
                    )
                    if record["status"] == "partial" and on_item_failure == "fail_task":
                        raise ValueError("partial batch failure requires task failure")
                except BaseException:
                    if record["status"] == "pending":
                        record["status"] = "failed"
                        record["outcomes"] = [
                            {
                                "item_id": item.item_id,
                                "status": "unknown",
                                "error_code": "batch_error",
                                "output_hash": None,
                            }
                            for item in batch
                        ]
                        unknown += len(batch)
                    raise
                finally:
                    record["latency_ms"] = (perf_counter() - began) * 1000
                    latencies.append(record["latency_ms"])
            receipt["state"] = "completed"
            if not usages:
                return ExperimentResult([])
            return ExperimentResult(outputs, usage=_sum_usage(usages))
        except BaseException:
            receipt["state"] = "failed"
            raise
        finally:
            receipt["latency_distribution_ms"] = summarize_values(latencies)
            planned = len(items) if isinstance(items, list) else None
            remaining = None if planned is None else planned - completed - failed - unknown
            publish_structural_observations(
                receipt,
                {
                    "batch_state": observation(receipt["state"], unit="state", source_ref=source),
                    "batch_size": observation(
                        constraints.batch_size, kind="declared", unit="items", source_ref=source
                    ),
                    "batch_invocations": observation(invoked, unit="calls", source_ref=source),
                    "batch_items_planned": observation(planned, unit="items", source_ref=source),
                    "batch_items_completed": observation(
                        completed, unit="completed_items", source_ref=source
                    ),
                    "batch_items_failed": observation(failed, unit="items", source_ref=source),
                    "batch_items_unknown": observation(unknown, unit="items", source_ref=source),
                    "batch_items_not_started": observation(
                        remaining, unit="items", source_ref=source
                    ),
                    "batch_latency_p95_ms": observation(
                        receipt["latency_distribution_ms"]["p95"], unit="ms", source_ref=source
                    ),
                },
            )

    return ExperimentRunner(
        candidate_id,
        "1.0",
        "agentloop.batching:1.0",
        "sha256:" + config_hash,
        invoke,
        reservation=reservation or Reservation(),
    )
