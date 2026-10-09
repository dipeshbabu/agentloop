"""Source-bound coordination observations; unknown boundaries stay unknown."""

from __future__ import annotations

from collections import Counter, defaultdict
from hashlib import sha256
from pathlib import Path
from typing import Iterable

from agentloop.graph import ExecutionGraph
from agentloop.integrations.omnigent.evidence import read_observations
from agentloop.interoperability.artifacts import native_bytes, write_artifact
from agentloop.interoperability.contracts import ImportReceipt
from agentloop.interoperability.evidence import read_external
from agentloop.interoperability.validation import ImportValidationError
from agentloop.interventions import canonical_json
from agentloop.timing import event_interval_ms, timestamp_ms
from agentloop.tracer import AgentTrace


def _fail(reason: str) -> None:
    raise ImportValidationError("invalid_coordination", "coordination", reason)


def _scope(receipt: dict) -> tuple:
    identity = receipt["external_identity"]
    return (
        receipt["source"]["system"],
        identity["job_id"],
        identity["trial_id"],
        identity["task_digest"] or receipt["source"]["artifact_sha256"],
    )


def _trajectory_scope(receipt: dict) -> tuple:
    identity = receipt["external_identity"]
    return (
        receipt["source"]["system"],
        identity["job_id"],
        identity["trial_id"],
        identity["task_digest"],
    )


def _overlap(intervals: list[tuple[float, float]]) -> float | None:
    if not intervals:
        return None
    changes = Counter()
    for start, end in intervals:
        changes[start] += 1
        changes[end] -= 1
    active, overlap, previous = 0, 0.0, None
    for moment, change in sorted(changes.items()):
        if previous is not None and active >= 2:
            overlap += moment - previous
        active += change
        previous = moment
    return overlap


def _captured_arguments(value) -> bool:
    if not isinstance(value, dict):
        return False
    pending = [value]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            if node.get("capture") == "omitted":
                return False
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return True


def _aggregate(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    keys = {
        "total_prompt_tokens",
        "total_completion_tokens",
        "total_cached_tokens",
        "total_cost_usd",
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "cost_usd",
    }
    result = {
        key: item
        for key, item in value.items()
        if key in keys and (item is None or type(item) in {int, float})
    }
    extra = value.get("extra")
    usage_scope = extra.get("usage_scope") if isinstance(extra, dict) else None
    if isinstance(usage_scope, str) and usage_scope in {
        "includes_subagents",
        "agent_and_subagents",
        "agent_only",
        "trajectory_only",
        "inclusive",
        "exclusive",
    }:
        result["usage_scope"] = usage_scope
    return result


def _critical_path(trace: AgentTrace, receipt: dict) -> dict:
    graph = ExecutionGraph.from_trace(trace)
    nodes = {node.node_id: node for node in graph.nodes}
    roots = [node for node in graph.nodes if node.parent_id is None]
    complete = (
        bool(nodes)
        and len(nodes) == len(graph.nodes)
        and len(roots) == 1
        and receipt["source_metadata"].get("conflicting_segments", 0) == 0
        and all(
            event.metadata.get("timing_available") is True
            and event.metadata.get("timing_provenance") == "external_reported"
            for event in trace.events
        )
        and all(event_interval_ms(node) is not None for node in graph.nodes)
        and all(node.parent_id is None or node.parent_id in nodes for node in graph.nodes)
        and all(not event.metadata.get("unresolved_parent_span_id") for event in trace.events)
    )
    # Native graph traversal detects cycles without inventing sequence edges for
    # qualified imports. Every path must reach the sole recorded root.
    if complete:
        visited, pending = set(), [roots[0].node_id]
        children = defaultdict(list)
        for node in graph.nodes:
            if node.parent_id:
                children[node.parent_id].append(node.node_id)
        while pending:
            identity = pending.pop()
            if identity in visited:
                complete = False
                break
            visited.add(identity)
            pending.extend(children[identity])
        complete = complete and len(visited) == len(nodes)
    value = graph.critical_path().to_dict() if complete else None
    return {
        "critical_path_ms": value["duration_ms"] if value else None,
        "node_ids": value["node_ids"] if value else [],
        "coverage": "recorded_connected_span_tree" if value else "unavailable",
        "scope": "this source trace's explicit span tree; not complete cross-process task causality",
    }


def summarize_coordination(traces: Iterable[AgentTrace], receipts: Iterable[ImportReceipt]) -> dict:
    """Join captured projections/receipts without merging trace or session IDs."""
    traces, receipts = tuple(traces), tuple(receipts)
    documents = [receipt.to_dict() for receipt in receipts]
    if len({row["receipt_id"] for row in documents}) != len(documents):
        _fail("duplicate source receipt identity")
    trace_index = {trace.run_id: trace for trace in traces}
    if len(trace_index) != len(traces):
        _fail("duplicate native trace identity")
    bindings, receipt_by_run = {}, {}
    for receipt in documents:
        for captured in receipt["traces"]:
            identity = captured["run_id"]
            if identity in bindings:
                _fail("multiple receipts claim one native trace")
            bindings[identity] = captured["trace_sha256"]
            receipt_by_run[identity] = receipt
    for trace in traces:
        if sha256(native_bytes(trace)).hexdigest() != bindings.get(trace.run_id):
            _fail("native projection differs from captured receipt")

    actors, actor_index, trajectory_index, event_actors = [], {}, {}, {}
    relationships, per_trace, raw_aggregates, models, tools = [], [], [], [], []
    outcomes = {}
    for row in documents:
        if row["source"]["format"] == "harbor_trial":
            key = (row["external_identity"]["job_id"], row["external_identity"]["trial_id"])
            if key in outcomes:
                _fail("ambiguous parent trial identity")
            outcomes[key] = row
    for trace in traces:
        receipt = receipt_by_run[trace.run_id]
        identity, source = receipt["external_identity"], receipt["source"]
        if source["format"] == "harbor_trial":
            # Agent-phase usage already includes trajectory/subagent usage.
            raw_aggregates.append(
                {
                    "receipt_id": receipt["receipt_id"],
                    "scope": "trial_agent_context",
                    "metrics": _aggregate(receipt["source_metadata"].get("usage")),
                }
            )
            continue
        external = read_external(trace)
        if external is None:
            _fail("coordination requires qualified source projections")
        observed = read_observations(trace)
        if observed:
            relationships.extend(
                {"run_id": trace.run_id, **row} for row in observed["relationships"]
            )
        per_trace.append({"run_id": trace.run_id, **_critical_path(trace, receipt)})
        aggregate = receipt["source_metadata"].get("source_final_metrics")
        if aggregate is not None:
            raw_aggregates.append(
                {
                    "receipt_id": receipt["receipt_id"],
                    "scope": "source_final_metrics_may_include_children",
                    "metrics": _aggregate(aggregate),
                }
            )
        raw_aggregates.extend(
            {
                "receipt_id": receipt["receipt_id"],
                "scope": "source_aggregate",
                "metrics": _aggregate(item),
            }
            for item in receipt["source_metadata"].get("source_aggregates", [])
        )

        atif = source["format"] == "atif"
        selected = (
            [None] if atif else [event for event in trace.events if event.operation_kind == "agent"]
        )
        for event in selected:
            actor_id = trace.run_id if event is None else f"{trace.run_id}/{event.event_id}"
            aliases = event.metadata.get("source.omnigent", {}) if event else {}
            interval = event_interval_ms(event) if event else None
            if event and (
                event.metadata.get("timing_provenance") != "external_reported"
                or receipt["source_metadata"].get("conflicting_segments", 0)
            ):
                interval = None
            if atif and external["runtime_ms"] is not None:
                start, end = timestamp_ms(trace.started_at), timestamp_ms(trace.ended_at)
                if start is not None and end is not None and end >= start:
                    interval = (start, end)
            agent = receipt["source_metadata"].get("agent") or {}
            trial = outcomes.get((identity["job_id"], identity["trial_id"]))
            status = (
                receipt["outcome"]["execution_status"]
                if event is None
                else event.metadata.get("source_execution_status", "unknown")
            )
            actor = {
                "actor_id": actor_id,
                "run_id": trace.run_id,
                "event_id": event.event_id if event else None,
                "source_receipt_id": receipt["receipt_id"],
                "source_identity": identity,
                "agent_name": aliases.get("agent_name") or agent.get("name"),
                "declared_role": aliases.get("agent_role")
                or (agent.get("extra") or {}).get("role"),
                "agent_id": aliases.get("agent_id"),
                "parent_agent_id": aliases.get("parent_agent_id"),
                "session_id": aliases.get("session_id") or identity["session_id"],
                "parent_session_id": aliases.get("parent_session_id"),
                "execution_status": status,
                "runtime_ms": interval[1] - interval[0] if interval else None,
                "timing_basis": "external_reported_interval" if interval else "unknown",
                "task_outcome": trial["outcome"] if trial else receipt["outcome"],
                "outcome_scope": "parent trial; child contribution not separately verified"
                if trial
                else "source receipt; role labels do not establish correctness",
            }
            if not isinstance(actor["declared_role"], str):
                actor["declared_role"] = None
            actors.append(actor)
            actor_index[actor_id] = (actor, interval)
            if atif:
                key = (*_trajectory_scope(receipt), identity["trajectory_id"])
                if key in trajectory_index:
                    _fail("ambiguous trajectory document identity")
                trajectory_index[key] = actor_id
            else:
                event_actors[(trace.run_id, event.event_id)] = actor_id
        models.extend(
            (trace, event, receipt) for event in trace.events if event.operation_kind == "model"
        )
        tools.extend(
            (trace, event, receipt) for event in trace.events if event.operation_kind == "tool"
        )

    alias_index = defaultdict(list)
    for actor in actors:
        identity = actor["source_identity"]
        receipt = receipt_by_run[actor["run_id"]]
        namespace = (receipt["source"]["system"], identity["job_id"], identity["trial_id"])
        for agent_id, session_id in {
            (actor["agent_id"], actor["session_id"]),
            (actor["agent_id"], None),
            (None, actor["session_id"]),
        }:
            if agent_id is not None or session_id is not None:
                alias_index[(*namespace, agent_id, session_id)].append(actor["actor_id"])
    for actor in actors:
        if actor["parent_agent_id"] is None and actor["parent_session_id"] is None:
            continue
        identity = actor["source_identity"]
        receipt = receipt_by_run[actor["run_id"]]
        matches = alias_index.get(
            (
                receipt["source"]["system"],
                identity["job_id"],
                identity["trial_id"],
                actor["parent_agent_id"],
                actor["parent_session_id"],
            ),
            [],
        )
        resolved = len(matches) == 1 and matches[0] != actor["actor_id"]
        relationships.append(
            {
                "kind": "declared_parent_identity",
                "source_actor_id": actor["actor_id"],
                "target_actor_id": matches[0] if resolved else None,
                "target_count": len(matches),
                "resolved": resolved,
                "causal_edge": False,
                "basis": "external_reported_identity; no dispatch or dependency boundary",
            }
        )

    handoffs = []
    for trace in traces:
        receipt = receipt_by_run[trace.run_id]
        if receipt["source"]["format"] == "atif":
            for relation in receipt["relationships"]:
                if relation["kind"] != "delegation":
                    continue
                target = (
                    trajectory_index.get((*_trajectory_scope(receipt), relation["target_id"]))
                    if relation["resolved"]
                    else None
                )
                handoffs.append(
                    {
                        "parent_actor_id": trace.run_id,
                        "child_actor_id": target,
                        "target_id": relation["target_id"],
                        "basis": "external_reported_trajectory_delegation",
                        "causal_edge": False,
                    }
                )
        else:
            events = {event.event_id: event for event in trace.events}
            ancestor_cache = {}

            def parent_actor(event_id):
                path, seen, current = [], set(), event_id
                result = None
                while current is not None and current not in seen:
                    if current in ancestor_cache:
                        result = ancestor_cache[current]
                        break
                    found = event_actors.get((trace.run_id, current))
                    if found:
                        result = found
                        break
                    seen.add(current)
                    path.append(current)
                    ancestor = events.get(current)
                    current = ancestor.parent_id if ancestor else None
                for key in path:
                    ancestor_cache[key] = result
                return result

            for event in trace.events:
                child = event_actors.get((trace.run_id, event.event_id))
                if child and event.parent_id:
                    parent = events.get(event.parent_id)
                    if parent:
                        handoffs.append(
                            {
                                "parent_actor_id": parent_actor(parent.event_id),
                                "parent_event_id": parent.event_id,
                                "child_actor_id": child,
                                "target_id": event.event_id,
                                "basis": "explicit_source_span_parent",
                                "causal_edge": True,
                            }
                        )
                    else:
                        relationships.append(
                            {
                                "run_id": trace.run_id,
                                "kind": "missing_span_parent",
                                "source_event_id": event.event_id,
                                "resolved": False,
                                "causal_edge": False,
                            }
                        )

    child_ids = {row["child_actor_id"] for row in handoffs if row["child_actor_id"]}
    parents = {row["parent_actor_id"] for row in handoffs if row["parent_actor_id"]}
    for row in handoffs:
        actor, interval = actor_index.get(row["child_actor_id"], (None, None))
        row.update(
            child_runtime_ms=actor["runtime_ms"] if actor else None,
            handoff_wait_ms=None,
            wait_missing_reason="no recorded dispatch/blocking-join interval; child runtime is not caller wait",
        )
    leaves = [pair for key, pair in actor_index.items() if key not in parents]
    timed = [interval for _, interval in leaves if interval is not None]
    overlap = _overlap(timed)

    model_parents = {
        (trace.run_id, event.parent_id) for trace, event, _ in models if event.parent_id
    }
    leaf_models = [
        (trace, event, receipt)
        for trace, event, receipt in models
        if (trace.run_id, event.event_id) not in model_parents
    ]
    known_usage = {}
    for direction in ("input", "output"):
        selected = [
            getattr(event, direction + "_tokens")
            for _, event, _ in leaf_models
            if event.metadata.get(direction + "_tokens_available") is True
        ]
        known_usage["known_" + direction + "_tokens"] = sum(selected) if selected else None
        known_usage[direction + "_tokens"] = (
            sum(selected) if leaf_models and len(selected) == len(models) else None
        )

    cache_values = []
    for _, event, receipt in leaf_models:
        cache = (
            event.metadata.get("source_metrics", {}).get("cached_tokens")
            if receipt["source"]["format"] == "atif"
            else event.metadata.get("cached_input_tokens")
        )
        if type(cache) is int:
            cache_values.append(cache)
    known_usage["known_cached_input_tokens"] = sum(cache_values) if cache_values else None
    known_usage["cached_input_tokens"] = (
        sum(cache_values) if leaf_models and len(cache_values) == len(models) else None
    )

    duplicate_groups = defaultdict(list)
    supported_tools = 0
    for trace, event, receipt in tools:
        arguments = event.metadata.get("source_arguments")
        if receipt["source"]["format"] == "atif" and _captured_arguments(arguments):
            supported_tools += 1
            digest = sha256(canonical_json(arguments).encode()).hexdigest()
            duplicate_groups[(*_scope(receipt), event.name, digest)].append(
                {"run_id": trace.run_id, "event_id": event.event_id}
            )
    duplicates = [
        {
            "tool_name": key[-2],
            "arguments_sha256": key[-1],
            "spans": spans,
            "basis": "identical_captured_arguments_same_named_tool_in_source_scope",
            "confidence": "medium",
            "safe_reuse": "unestablished",
            "estimated_savings_ms": None,
            "estimated_savings_usd": None,
        }
        for key, spans in sorted(duplicate_groups.items(), key=lambda item: str(item[0]))
        if len(spans) > 1
    ]
    known_duplicates = sum(len(row["spans"]) - 1 for row in duplicates)
    reported_counts = [
        read_external(trace)["reported_model_call_count"]
        for trace in traces
        if receipt_by_run[trace.run_id]["source"]["format"] != "harbor_trial"
    ]
    unknown_children = sum(row["child_actor_id"] is None for row in handoffs)
    return {
        "schema_version": "1.0",
        "source_receipt_ids": sorted(row["receipt_id"] for row in documents),
        "actors": actors,
        "handoffs": handoffs,
        "relationships": relationships,
        "handoff_count": len(handoffs),
        "observed_child_count": len(child_ids),
        "unknown_child_count": unknown_children,
        "unknown_parent_reference_count": sum(
            row.get("resolved") is False
            and row["kind"]
            in {"missing_span_parent", "declared_parent_identity", "ended_parent_reference"}
            for row in relationships
        ),
        "child_failure_count": sum(
            actor_index[key][0]["execution_status"] in {"failed", "timed_out", "cancelled"}
            for key in child_ids
        ),
        "child_status_unknown_count": sum(
            actor_index[key][0]["execution_status"] == "unknown" for key in child_ids
        ),
        "handoff_wait_ms": None,
        "handoff_wait_coverage": {"measured": 0, "declared": len(handoffs)},
        "parallel_overlap_ms": overlap
        if len(timed) == len(leaves) and not unknown_children
        else None,
        "known_parallel_overlap_ms": overlap,
        "overlap_coverage": {
            "timed_leaf_actors": len(timed),
            "observed_leaf_actors": len(leaves),
            "unknown_children": unknown_children,
        },
        "overlap_basis": "union of times with at least two recorded leaf-actor intervals; enclosing parent actors excluded; overlap does not prove independent work or synchronized source clocks",
        "critical_path_ms": per_trace[0]["critical_path_ms"]
        if len(per_trace) == 1 and not unknown_children and not relationships
        else None,
        "critical_path_by_trace": per_trace,
        "model_call_count": sum(reported_counts)
        if reported_counts and all(value is not None for value in reported_counts)
        else None,
        "model_call_count_basis": "reported multiplicity for recorded source blocks; complete executor coverage not established",
        "observed_model_blocks": len(models),
        "retry_count": None,
        "usage": {
            **known_usage,
            "raw_inclusive_aggregates": raw_aggregates,
            "aggregation_basis": "unique native source documents/spans, model leaves only; inclusive/root/trial aggregates retained separately and never added",
            "coverage": "recorded_model_leaves_only",
            "provider_billing_verified": False,
        },
        "possible_duplicate_work_count": known_duplicates
        if tools and supported_tools == len(tools)
        else None,
        "known_possible_duplicate_work_count": known_duplicates,
        "possible_duplicate_work": duplicates,
        "duplicate_work_coverage": {
            "captured_tool_inputs": supported_tools,
            "observed_tools": len(tools),
        },
        "review_overhead_ms": None,
        "review_quality_delta": None,
        "review_missing_reason": "requires controlled reviewed-vs-unreviewed task pairs; reviewer labels do not establish a quality effect",
        "limitations": [
            "source hashes bind supplied bytes, not authenticity",
            "missing child logs, timing, retries and contributions remain unknown",
            "session IDs and span links correlate observations without creating causal dependencies",
            "identical tool inputs can require independent verification or have changing state/side effects; investigate with the existing semantic redundancy and paired-intervention contracts",
        ],
        "synthetic": any(trace.metadata.get("synthetic") is True for trace in traces),
    }


def write_coordination_report(traces, receipts, out: str | Path) -> Path:
    from agentloop.html_report import coordination_to_html

    report = summarize_coordination(traces, receipts)
    root = Path(out)
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    write_artifact(root, "coordination.json", (canonical_json(report) + "\n").encode())
    write_artifact(root, "coordination.html", coordination_to_html(report).encode())
    return root / "coordination.json"
