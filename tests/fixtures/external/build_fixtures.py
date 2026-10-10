"""Regenerate owned synthetic examples; no upstream runtime or network needed."""

from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

from agentloop.interoperability.contracts import (
    IDENTITY_FIELDS,
    ImportReceipt,
    receipt_id,
    summarize_receipts,
)

ROOT = Path(__file__).parent
HARBOR_REVISION = "07ad34000e4c481451b1a0ea30a4a09548d6de8b"
OMNIGENT_REVISION = "713673e9d48cf09cb71fd3a90ae45856ada288d1"


def write(path: str, value: object) -> str:
    data = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    out = ROOT / path
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return sha256(data).hexdigest()


def trajectory(name: str, version: str = "ATIF-v1.7") -> dict:
    return {
        "schema_version": version,
        "trajectory_id": name,
        "session_id": "shared-session",
        "agent": {"name": "synthetic-agent", "version": "fixture-1", "model_name": "fixture-model"},
        "steps": [
            {"step_id": 1, "source": "user", "message": "Count the blue cards."},
            {
                "step_id": 2,
                "source": "agent",
                "message": "There are two blue cards.",
                "timestamp": "2026-01-01T00:00:01Z",
                "llm_call_count": 1,
                "metrics": {"prompt_tokens": 12, "completion_tokens": 6, "cached_tokens": 4},
            },
        ],
        "final_metrics": {
            "total_prompt_tokens": 12,
            "total_completion_tokens": 6,
            "total_cached_tokens": 4,
        },
    }


def attributes(values: dict) -> list:
    return [
        {
            "key": key,
            "value": {
                "boolValue"
                if type(value) is bool
                else "intValue"
                if type(value) is int
                else "stringValue": str(value) if type(value) is int else value
            },
        }
        for key, value in values.items()
    ]


def span(trace: str, event: str, kind: str, *, parent: str | None = None, **attrs) -> dict:
    result = {
        "traceId": trace * 32,
        "spanId": event * 16,
        "name": kind.lower(),
        "startTimeUnixNano": "1767225600000000000",
        "endTimeUnixNano": "1767225601000000000",
        "attributes": attributes(
            {"openinference.span.kind": kind, "session.id": "shared-session", **attrs}
        ),
    }
    if parent:
        result["parentSpanId"] = parent * 16
    return result


def otlp(spans: list, service: str = "omnigent") -> dict:
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": attributes({"service.name": service})},
                "scopeSpans": [{"scope": {"name": service}, "spans": spans}],
            }
        ]
    }


def main() -> None:
    families = []
    files = {}

    def freeze(family: str, path: str, payload: object, expected: dict) -> None:
        files[path] = write(path, payload)
        families.append({"family": family, "path": path, "expected": expected})

    simple = trajectory("simple")
    freeze(
        "atif_v17",
        "harbor/atif_v17_simple.json",
        simple,
        {
            "model_calls": 1,
            "input_tokens": 12,
            "cached_subset": 4,
            "timing": "missing",
            "cost": "missing",
        },
    )
    media = trajectory("multimodal", "ATIF-v1.8")
    media["steps"][0]["message"] = [
        {"type": "text", "text": "Read the card and listen to the instruction."},
        {"type": "image", "source": {"media_type": "image/png", "path": "media/card.png"}},
        {
            "type": "audio",
            "source": {
                "media_type": "audio/wav",
                "path": "https://example.invalid/instruction.wav",
                "duration_sec": 1.5,
            },
        },
    ]
    media["steps"][1]["reasoning_content"] = "SYNTHETIC_REASONING_MUST_BE_OMITTED"
    media["steps"][1]["metrics"]["prompt_token_ids"] = [1, 2, 3]
    freeze(
        "atif_v18_multimodal",
        "harbor/atif_v18_multimodal.json",
        media,
        {
            "references_read": 0,
            "reasoning_exported": False,
            "token_ids_exported": False,
            "audio_is_model_latency": False,
        },
    )
    embedded = trajectory("parent")
    embedded["steps"][1]["tool_calls"] = [
        {"tool_call_id": "delegate-1", "function_name": "delegate", "arguments": {}}
    ]
    embedded["steps"][1]["observation"] = {
        "results": [
            {
                "source_call_id": "delegate-1",
                "subagent_trajectory_ref": [
                    {"trajectory_id": "child-a"},
                    {"trajectory_id": "child-b"},
                ],
            }
        ]
    }
    embedded["subagent_trajectories"] = [trajectory("child-a"), trajectory("child-b")]
    embedded["final_metrics"] = {
        "total_prompt_tokens": 36,
        "total_completion_tokens": 18,
        "extra": {"usage_scope": "includes_subagents"},
    }
    freeze(
        "atif_shared_session_subagents",
        "harbor/atif_embedded_subagents.json",
        embedded,
        {
            "documents": 3,
            "distinct_native_run_ids": 3,
            "session_groups": 1,
            "aggregate_is_additive": False,
        },
    )
    continuation = trajectory("continuation-root")
    continuation["continued_trajectory_ref"] = "continuation_segment.json"
    files["harbor/continuation_segment.json"] = write(
        "harbor/continuation_segment.json",
        {
            **trajectory("continued"),
            "steps": [
                {
                    "step_id": 1,
                    "source": "system",
                    "message": "Copied context summary",
                    "is_copied_context": True,
                },
                {"step_id": 2, "source": "agent", "message": "Done", "llm_call_count": 1},
            ],
        },
    )
    freeze(
        "atif_continuation",
        "harbor/atif_continuation.json",
        continuation,
        {"documents": 2, "copied_model_calls": 0, "continuation_is_causal_dependency": False},
    )
    deterministic = trajectory("deterministic")
    deterministic["steps"] = [
        {
            "step_id": 1,
            "source": "agent",
            "message": "Dispatch lookup",
            "llm_call_count": 0,
            "tool_calls": [
                {"tool_call_id": "lookup-1", "function_name": "lookup", "arguments": {}}
            ],
        }
    ]
    deterministic.pop("final_metrics")
    freeze(
        "atif_deterministic",
        "harbor/atif_deterministic_dispatch.json",
        deterministic,
        {"model_calls": 0, "tool_results": "missing", "timing": "missing"},
    )
    aggregated = trajectory("aggregated")
    aggregated["steps"][1]["llm_call_count"] = 3
    freeze(
        "atif_aggregated",
        "harbor/atif_aggregated_llm_calls.json",
        aggregated,
        {"model_blocks": 1, "source_call_multiplicity": 3, "fabricated_per_call_spans": 0},
    )

    # Result fields follow TrialResult. Config/lock are intentionally projections
    # of the upstream contracts, not an executable Harbor job configuration.
    job_path = "harbor/mixed_job"
    files[job_path + "/config.json"] = write(
        job_path + "/config.json", {"job_name": "synthetic-job", "n_attempts": 1}
    )
    trial_names = [
        "pass",
        "quality_fail",
        "exception",
        "timeout",
        "cancelled",
        "missing_trajectory",
        "uninterpreted",
        "multistep",
    ]
    for index, name in enumerate(trial_names, 1):
        trial = f"{job_path}/{name}"
        config = {
            "task": {"path": "tasks/blue-card"},
            "agent": {"name": "fixture", "model_name": "fixture-model"},
            "environment": {"type": "docker"},
            "verifier": {"disable": False},
        }
        lock = {
            "schema_version": 2,
            "task": {"name": "blue-card", "type": "local", "digest": "sha256:" + "a" * 64},
            "agent": config["agent"],
            "environment": config["environment"],
            "verifier": {"environment_mode": "shared" if name == "uninterpreted" else "separate"},
        }
        result = {
            "id": f"00000000-0000-0000-0000-{index:012d}",
            "task_name": "blue-card",
            "trial_name": name,
            "trial_uri": f"file:///synthetic/{name}",
            "task_id": {"path": "tasks/blue-card"},
            "task_checksum": "a" * 64,
            "config": config,
            "agent_info": {
                "name": "fixture",
                "version": "1",
                "model_info": {"name": "fixture-model"},
            },
            "agent_execution": {
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:00:02Z",
            },
            "verifier_result": {
                "rewards": {
                    "correctness": 0
                    if name == "quality_fail"
                    else 0.5
                    if name == "uninterpreted"
                    else 1.0
                }
            },
            "verifier_environment_mode": lock["verifier"]["environment_mode"],
        }
        if name in {"exception", "timeout", "cancelled"}:
            result["exception_info"] = {
                "exception_type": {
                    "exception": "AgentError",
                    "timeout": "AgentTimeoutError",
                    "cancelled": "CancelledError",
                }[name],
                "exception_message": "SYNTHETIC_ERROR_MESSAGE_MUST_BE_REDACTED",
                "exception_traceback": "SYNTHETIC_STACK_MUST_BE_REDACTED",
                "occurred_at": "2026-01-01T00:00:02Z",
            }
            result["verifier_result"] = None
        if name == "multistep":
            result["verifier_environment_mode"] = None
            result["verifier_result"] = None
            result["agent_execution"] = None
            result["step_results"] = [
                {"step_name": "prepare", "verifier_result": {"rewards": {"correctness": 1.0}}},
                {"step_name": "answer", "verifier_result": {"rewards": {"correctness": 0.0}}},
            ]
            for step, mode in (("prepare", "shared"), ("answer", "separate")):
                files[f"{trial}/steps/{step}/agent/trajectory.json"] = write(
                    f"{trial}/steps/{step}/agent/trajectory.json", trajectory(step)
                )
                files[f"{trial}/steps/{step}/config.json"] = write(
                    f"{trial}/steps/{step}/config.json", {"verifier": {"environment_mode": mode}}
                )
        elif name not in {"exception", "timeout", "cancelled", "missing_trajectory"}:
            files[trial + "/agent/trajectory.json"] = write(
                trial + "/agent/trajectory.json", trajectory(name)
            )
        for artifact, payload in (
            ("config.json", config),
            ("lock.json", lock),
            ("result.json", result),
        ):
            files[trial + "/" + artifact] = write(trial + "/" + artifact, payload)
        if result["verifier_result"]:
            files[trial + "/verifier/reward.json"] = write(
                trial + "/verifier/reward.json", result["verifier_result"]["rewards"]
            )
    families.append(
        {
            "family": "harbor_mixed_job",
            "path": job_path,
            "expected": {
                "trials": 8,
                "single_step_trajectories": 3,
                "step_trajectories": 2,
                "quality_without_contract": None,
                "task_code_executed": False,
                "config_contract": "projection",
            },
        }
    )

    policy = span(
        "a",
        "3",
        "GUARDRAIL",
        parent="1",
        **{"policy.name": "read-only", "policy.phase": "pre_tool", "policy.action": "DENY"},
    )
    agent_tool = otlp(
        [
            span(
                "a",
                "1",
                "AGENT",
                **{"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "fixture-agent"},
            ),
            span(
                "a",
                "2",
                "TOOL",
                parent="1",
                **{"gen_ai.operation.name": "execute_tool", "tool.name": "lookup"},
            ),
            policy,
        ]
    )
    freeze(
        "omnigent_policy",
        "omnigent/agent_tool_policy.otlp.json",
        agent_tool,
        {
            "operations": {"agent": 1, "tool": 1, "guardrail": 1},
            "policy_action": "DENY",
            "pre_dispatch_prevention_proven": False,
            "llm_coverage": "unknown",
        },
    )
    child = span("b", "4", "AGENT", **{"gen_ai.agent.name": "child"})
    child["links"] = [
        {
            "traceId": "a" * 32,
            "spanId": "1" * 16,
            "attributes": attributes({"relationship": "delegation"}),
        }
    ]
    freeze(
        "omnigent_multi_trace_session",
        "omnigent/parent_child_multi_trace.otlp.json",
        otlp([span("a", "1", "AGENT"), child]),
        {"traces": 2, "session_groups": 1, "cross_trace_parent_edges": 0, "span_links": 1},
    )
    freeze(
        "omnigent_unverified_policy",
        "omnigent/unverified_policy_decision.otlp.json",
        otlp([policy]),
        {"policy_action": "DENY", "dispatch_observed": False, "enforcement": "unknown"},
    )
    freeze(
        "omnigent_missing_parent",
        "omnigent/missing_parentage.otlp.json",
        otlp([span("a", "2", "TOOL", parent="f")]),
        {"unresolved_parents": 1, "fabricated_parents": 0},
    )
    jsonl_payloads = [
        otlp([span("a", "1", "AGENT")], "harbor"),
        otlp(
            [
                span(
                    "b",
                    "2",
                    "LLM",
                    **{"llm.token_count.prompt": 12, "llm.token_count.completion": 6},
                )
            ],
            "harbor",
        ),
    ]
    for family, path, lines, expected in (
        (
            "otlp_two_records",
            "otlp/two_records.jsonl",
            [json.dumps(value) for value in jsonl_payloads],
            {"records": 2, "traces": 2},
        ),
        (
            "otlp_partial_records",
            "otlp/malformed_mixed_valid.jsonl",
            ["{INVALID_JSON", json.dumps(jsonl_payloads[1])],
            {"invalid_records": 1, "valid_later_records": 1},
        ),
        (
            "otlp_duplicate_segments",
            "otlp/duplicate_trace_ids.jsonl",
            [
                json.dumps(jsonl_payloads[0]),
                json.dumps(jsonl_payloads[0]),
                json.dumps(otlp([span("a", "5", "TOOL", parent="1")], "harbor")),
            ],
            {"records": 3, "traces": 1, "identical_duplicate_spans": 1, "unique_spans": 2},
        ),
    ):
        out = ROOT / path
        out.parent.mkdir(parents=True, exist_ok=True)
        data = ("\n".join(lines) + "\n").encode("utf-8")
        out.write_bytes(data)
        files[path] = sha256(data).hexdigest()
        families.append({"family": family, "path": path, "expected": expected})

    hostile = {
        "json_cases": [
            {"data": '{"secret":"FIRST","secret":"SECOND"}', "code": "duplicate_key"},
            {"data": '{"value":NaN}', "code": "invalid_number"},
            {"data": '{"value":1e999}', "code": "invalid_number"},
            {"data": '"\\ud800"', "code": "invalid_json"},
        ],
        "unsafe_references": [
            "../outside.json",
            "/absolute.json",
            "C:/private.json",
            "C:relative.json",
            "\\\\host\\share\\file.json",
            "https://example.invalid/data.json",
            "file:///private.json",
            "safe/../private.json",
            "safe//file.json",
            "NUL.json",
            "file.json:stream",
        ],
        "adapter_cases_pending": [
            {"mutation": "future_schema_version", "expected": "reject"},
            {"mutation": "duplicate_step_id", "expected": "reject"},
            {"mutation": "duplicate_embedded_document_id", "expected": "reject"},
            {"mutation": "invalid_source", "expected": "reject"},
            {"mutation": "negative_or_boolean_token_counts", "expected": "reject"},
            {"mutation": "continuation_cycle", "expected": "qualified_failure"},
            {"mutation": "missing_tool_observation", "expected": "qualified_receipt"},
        ],
    }
    freeze(
        "hostile_and_sparse_contracts",
        "hostile_cases.json",
        hostile,
        {
            "parser_rejects": True,
            "external_references_read": 0,
            "adapter_semantics": "pending dependent PRs",
        },
    )

    missing = {
        "schema_version": "1.0",
        "receipt_id": "computed below",
        "source": {
            "system": "harbor",
            "producer_version": "0.24.0",
            "producer_revision": HARBOR_REVISION,
            "format": "harbor_trial",
            "format_version": None,
            "artifact_reference": "exception/result.json",
            "artifact_sha256": files[job_path + "/exception/result.json"],
            "trust": "external_reported",
        },
        "external_identity": {key: None for key in sorted(IDENTITY_FIELDS)},
        "identity_provenance": {key: "unknown" for key in sorted(IDENTITY_FIELDS)},
        "traces": [],
        "missing_trace_reason": "missing_artifact",
        "outcome": {
            "execution_status": "failed",
            "verifier_status": "missing",
            "verifier_dimensions": {},
            "quality_pass": None,
            "quality_basis": "unavailable",
            "scoring_contract": None,
            "verifier_isolation": "unknown",
        },
        "completeness": {
            "trajectory": "missing",
            "usage": "unknown",
            "quality": "missing",
            "parentage": "unknown",
            "timing": "partial",
            "cost": "unknown",
        },
        "relationships": [],
        "notices": [
            {
                "code": "missing_trajectory",
                "severity": "warning",
                "artifact": "exception/result.json",
                "field": None,
                "record": None,
            }
        ],
        "source_metadata": {"exception_type": "AgentError", "synthetic": True},
    }
    missing["external_identity"]["trial_id"] = "00000000-0000-0000-0000-000000000003"
    missing["identity_provenance"]["trial_id"] = "external_reported"
    missing["receipt_id"] = receipt_id(missing)
    unknown = deepcopy(missing)
    unknown["source"]["artifact_reference"] = "uninterpreted/result.json"
    unknown["source"]["artifact_sha256"] = files[job_path + "/uninterpreted/result.json"]
    unknown["external_identity"]["trial_id"] = "00000000-0000-0000-0000-000000000007"
    unknown["missing_trace_reason"] = "untimed_operations"
    unknown["outcome"].update(
        execution_status="completed",
        verifier_status="available",
        verifier_dimensions={"correctness": 0.5},
        quality_basis="external_uninterpreted_reward",
        verifier_isolation="shared",
    )
    unknown["completeness"].update(
        trajectory="complete", usage="partial", quality="complete", timing="missing"
    )
    unknown["notices"] = [
        {
            "code": "untimed_operations",
            "severity": "warning",
            "artifact": "uninterpreted/agent/trajectory.json",
            "field": "steps",
            "record": None,
        }
    ]
    unknown["source_metadata"] = {"synthetic": True}
    unknown["receipt_id"] = receipt_id(unknown)
    receipts = [ImportReceipt.from_dict(value) for value in (missing, unknown)]
    files["expected/imported_receipts.json"] = write(
        "expected/imported_receipts.json", [value.to_dict() for value in receipts]
    )
    files["expected/counters_and_completeness.json"] = write(
        "expected/counters_and_completeness.json", summarize_receipts(receipts)
    )
    write(
        "source_matrix.json",
        {
            "schema_version": "1.0",
            "generated": "owned_synthetic",
            "inspected_at": "2026-10-09",
            "families": families,
            "sources": [
                {
                    "system": "agentloop",
                    "revision": "69cfcb1cf82061fb70229465e296d821f650283d",
                    "release": "v0.7.0",
                    "native_schema_version": "1.1",
                    "url": "https://github.com/dipeshbabu/agentloop",
                    "license": "Apache-2.0",
                },
                {
                    "system": "harbor",
                    "revision": HARBOR_REVISION,
                    "release": "v0.24.0",
                    "formats": ["ATIF-v1.7", "ATIF-v1.8", "OTLP JSON"],
                    "url": "https://github.com/harbor-framework/harbor",
                    "license": "Apache-2.0",
                },
                {
                    "system": "omnigent",
                    "revision": OMNIGENT_REVISION,
                    "release": "v0.17.0",
                    "formats": ["OTLP JSON"],
                    "url": "https://github.com/omnigent-ai/omnigent",
                    "license": "Apache-2.0",
                },
            ],
            "files_sha256": dict(sorted(files.items())),
        },
    )


if __name__ == "__main__":
    main()
