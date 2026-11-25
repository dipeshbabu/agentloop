"""Versioned execution IR with dependency and effect contracts (issue #396).

The IR is a typed, deterministic view over the existing execution graph. It
does not replace the trace model: every operation is a recorded span, and every
property says where it came from.

Each property is ``{"value": ..., "basis": ...}`` where ``basis`` is one of:

``declared``
    The integration stated it, under ``agentloop.effects`` or an existing
    declaration such as ``read_only``, ``parallel_safe`` or ``depends_on``.
``observed``
    Measured in this trace: status, timing, containment, concurrency.
``inferred``
    Derived by AgentLoop from ordering alone, such as sequence edges between
    root spans with no declared dependencies. Never authorizes anything.
``unknown``
    No evidence. ``value`` is ``None``.

Edges carry a ``kind`` and a ``basis``:

=============  ==========  =====================================================
kind           basis       meaning
=============  ==========  =====================================================
``contains``   observed    parent span contains child span
``data``       declared    target declares ``depends_on`` the source
``state``      declared    source writes a state key the target reads or writes
``sequence``   inferred    root spans ran one after another; order only
=============  ==========  =====================================================

:func:`independence` answers whether two operations may be reordered or run
concurrently. It requires declared effects and declared dependencies, and it
abstains whenever the only evidence is inferred, unknown or contradictory.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from agentloop.graph import ExecutionGraph
from agentloop.operations import normalize_operation_kind
from agentloop.source_provenance import SOURCE_KEY
from agentloop.timing import event_interval_ms
from agentloop.workflow_types import STAGE_KEY

IR_SCHEMA = "agentloop.ir/v1"
EFFECTS_KEY = "agentloop.effects"
LEGACY_GRAPH_SCHEMA = "agentloop.graph/legacy"
SUPPORTED_SCHEMAS = (IR_SCHEMA,)

EFFECTS = ("none", "read", "write", "external")
BASES = ("declared", "observed", "inferred", "unknown")
_STATE_KEY = re.compile(r"^[A-Za-z0-9_.:/@#\-]{1,256}$")
_SCOPE = re.compile(r"^[A-Za-z0-9_.:/@#\- ]{1,128}$")
_MAX_KEYS = 256


class IRVersionError(ValueError):
    """The document's schema is not one this version of AgentLoop can read."""


def _known(value: Any, basis: str, source: str | None = None) -> dict[str, Any]:
    prop: dict[str, Any] = {"value": value, "basis": basis}
    if source is not None:
        prop["source"] = source
    return prop


def _unknown() -> dict[str, Any]:
    return {"value": None, "basis": "unknown"}


def _state_keys(value: Any) -> list[str] | None:
    if not isinstance(value, list) or len(value) > _MAX_KEYS:
        return None
    if not all(isinstance(item, str) and _STATE_KEY.match(item) for item in value):
        return None
    return sorted(set(value))


def _declared_effects(metadata: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Read ``agentloop.effects`` and older declarations; report malformed fields."""
    problems: list[str] = []
    props: dict[str, Any] = {
        "effect": _unknown(),
        "idempotent": _unknown(),
        "deterministic": _unknown(),
        "reads": _unknown(),
        "writes": _unknown(),
        "authorization_scope": _unknown(),
        "freshness_scope": _unknown(),
    }
    if metadata.get("read_only") is True:
        props["effect"] = _known("read", "declared", "read_only")
    elif metadata.get("read_only") is False:
        props["effect"] = _known("write", "declared", "read_only")

    raw = metadata.get(EFFECTS_KEY)
    if raw is None:
        return props, problems
    if not isinstance(raw, Mapping):
        return props, [f"{EFFECTS_KEY} must be an object"]
    source = EFFECTS_KEY
    effect = raw.get("effect")
    if effect is not None:
        if effect in EFFECTS:
            previous = props["effect"]
            if (
                previous["basis"] == "declared"
                and previous["value"] != effect
                and not (previous["value"] == "write" and effect == "external")
            ):
                problems.append(
                    f"effect {effect!r} contradicts read_only={metadata.get('read_only')!r}"
                )
                props["effect"] = _unknown()
            else:
                props["effect"] = _known(effect, "declared", source)
        else:
            problems.append(f"effect must be one of {', '.join(EFFECTS)}")
    for key in ("idempotent", "deterministic"):
        if key in raw:
            if isinstance(raw[key], bool):
                props[key] = _known(raw[key], "declared", source)
            else:
                problems.append(f"{key} must be true or false")
    for key in ("reads", "writes"):
        if key in raw:
            keys = _state_keys(raw[key])
            if keys is None:
                problems.append(f"{key} must be a list of at most {_MAX_KEYS} state keys")
            else:
                props[key] = _known(keys, "declared", source)
    for key in ("authorization_scope", "freshness_scope"):
        if key in raw:
            value = raw[key]
            if isinstance(value, str) and _SCOPE.match(value):
                props[key] = _known(value, "declared", source)
            else:
                problems.append(f"{key} must be short text")
    unknown_fields = sorted(
        set(raw)
        - {
            "effect",
            "idempotent",
            "deterministic",
            "reads",
            "writes",
            "authorization_scope",
            "freshness_scope",
        }
    )
    if unknown_fields:
        problems.append(f"unrecognized {EFFECTS_KEY} fields: {', '.join(unknown_fields)}")
    effect_value = props["effect"]["value"]
    writes = props["writes"]["value"]
    if effect_value in {"none", "read"} and writes:
        problems.append(f"effect {effect_value!r} cannot declare writes")
        props["effect"] = _unknown()
    if effect_value == "none" and props["reads"]["value"]:
        problems.append("effect 'none' cannot declare reads")
        props["effect"] = _unknown()
    return props, problems


def build_execution_ir(trace: Any) -> dict[str, Any]:
    """Build the IR for a native trace (an ``AgentTrace`` or anything with ``events``)."""
    graph = ExecutionGraph.from_trace(trace)
    return _from_nodes(
        run_id=str(getattr(trace, "run_id", "") or ""),
        nodes=[
            {
                "node_id": node.node_id,
                "name": node.name,
                "event_type": node.event_type,
                "operation_kind": node.operation_kind,
                "parent_id": node.parent_id,
                "status": node.status,
                "started_at": node.started_at,
                "ended_at": node.ended_at,
                "duration_ms": node.duration_ms,
                "metadata": node.metadata,
                "interval": event_interval_ms(node),
            }
            for node in graph.nodes
        ],
        edges=[edge.to_dict() for edge in graph.edges],
        dependency_mode=graph.dependency_mode,
        input_schema="native",
    )


def ir_from_graph(graph: Mapping[str, Any], *, run_id: str = "") -> dict[str, Any]:
    """Migrate a stored report graph (``report["graph"]``) to the IR.

    Older reports kept only the graph dictionary. Timing intervals are rebuilt
    from ``started_at``/``ended_at``; anything missing stays unknown.
    """
    nodes = graph.get("nodes") if isinstance(graph, Mapping) else None
    edges = graph.get("edges") if isinstance(graph, Mapping) else None
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise IRVersionError("a legacy graph needs 'nodes' and 'edges' lists")
    prepared = []
    for node in nodes:
        if not isinstance(node, Mapping) or not isinstance(node.get("node_id"), str):
            raise IRVersionError("every legacy graph node needs a string node_id")
        prepared.append(
            {
                "node_id": node["node_id"],
                "name": str(node.get("name", "")),
                "event_type": str(node.get("event_type", "")),
                "operation_kind": normalize_operation_kind(node.get("operation_kind")),
                "parent_id": node.get("parent_id"),
                "status": node.get("status", "unknown"),
                "started_at": node.get("started_at"),
                "ended_at": node.get("ended_at"),
                "duration_ms": node.get("duration_ms"),
                "metadata": node.get("metadata")
                if isinstance(node.get("metadata"), Mapping)
                else {},
                "interval": _interval_from_dict(node),
            }
        )
    clean_edges = [
        edge
        for edge in edges
        if isinstance(edge, Mapping)
        and isinstance(edge.get("source"), str)
        and isinstance(edge.get("target"), str)
    ]
    declared = any(edge.get("kind") == "dependency" for edge in clean_edges)
    return _from_nodes(
        run_id=run_id,
        nodes=prepared,
        edges=clean_edges,
        dependency_mode="declared" if declared else None,
        input_schema=LEGACY_GRAPH_SCHEMA,
    )


def _interval_from_dict(node: Mapping[str, Any]) -> tuple[float, float] | None:
    class _Span:
        started_at = node.get("started_at")
        ended_at = node.get("ended_at")
        duration_ms = node.get("duration_ms")

    try:
        return event_interval_ms(_Span())
    except (TypeError, ValueError):
        return None


def _from_nodes(
    *,
    run_id: str,
    nodes: list[dict[str, Any]],
    edges: list[Mapping[str, Any]],
    dependency_mode: str | None,
    input_schema: str,
) -> dict[str, Any]:
    unsupported: list[dict[str, Any]] = []
    ids = [node["node_id"] for node in nodes]
    duplicates = sorted({node_id for node_id in ids if ids.count(node_id) > 1})
    for node_id in duplicates:
        unsupported.append({"operation": node_id, "reason": "duplicate span id"})
    known = set(ids)

    starts = [node["interval"][0] for node in nodes if node["interval"] is not None]
    origin = min(starts) if starts else 0.0
    operations: list[dict[str, Any]] = []
    instance_counter: dict[tuple[str, Any], int] = {}
    for node in nodes:
        metadata = node["metadata"] if isinstance(node["metadata"], Mapping) else {}
        props, problems = _declared_effects(metadata)
        for problem in problems:
            unsupported.append({"operation": node["node_id"], "reason": problem})
        key = (node["name"], node["parent_id"])
        instance_counter[key] = instance_counter.get(key, 0) + 1
        interval = node["interval"]
        operation = {
            "id": node["node_id"],
            "name": node["name"],
            "kind": node["operation_kind"],
            "parent": node["parent_id"] if node["parent_id"] in known else None,
            "status": _known(node["status"], "observed")
            if node["status"] in {"ok", "error"}
            else _unknown(),
            # Milliseconds from the earliest observed start in this trace.
            "timing": _known(
                {
                    "start_ms": round(interval[0] - origin, 3),
                    "end_ms": round(interval[1] - origin, 3),
                },
                "observed",
            )
            if interval is not None
            else _unknown(),
            # The nth recorded call of this name under the same parent: loop
            # iterations and retries appear as separate observed instances.
            "instance": instance_counter[key],
            **props,
        }
        if metadata.get("parallel_safe") is True:
            operation["parallel_safe"] = _known(True, "declared", "parallel_safe")
        elif "parallel_safe" in metadata:
            operation["parallel_safe"] = _known(False, "declared", "parallel_safe")
        else:
            operation["parallel_safe"] = _unknown()
        if node["parent_id"] and node["parent_id"] not in known:
            unsupported.append(
                {"operation": node["node_id"], "reason": "parent span is not in this trace"}
            )
        source = metadata.get(SOURCE_KEY)
        if isinstance(source, Mapping):
            operation["source"] = {
                key: source[key]
                for key in (
                    "path",
                    "qualname",
                    "line_start",
                    "line_end",
                    "content_sha256",
                    "status",
                )
                if key in source
            }
        operations.append(operation)

    declared_targets, declared_edges = _declared_dependencies(nodes, known, unsupported)
    out_edges: list[dict[str, Any]] = []
    for edge in [*edges, *declared_edges]:
        if edge.get("kind", "sequence") == "sequence" and edge["target"] in declared_targets:
            # The target declared its dependencies; ordering alone adds nothing.
            continue
        source, target, kind = edge["source"], edge["target"], edge.get("kind", "sequence")
        if source not in known or target not in known:
            unsupported.append(
                {"operation": target, "reason": f"{kind} edge references an unknown span {source}"}
            )
            continue
        if source == target:
            unsupported.append({"operation": target, "reason": "operation depends on itself"})
            continue
        mapped = {
            "parent": ("contains", "observed"),
            "dependency": ("data", "declared"),
            "sequence": ("sequence", "inferred"),
        }.get(kind)
        if mapped is None:
            unsupported.append({"operation": target, "reason": f"unknown edge kind {kind!r}"})
            continue
        out_edges.append(
            {"source": source, "target": target, "kind": mapped[0], "basis": mapped[1]}
        )

    order = {node_id: index for index, node_id in enumerate(ids)}
    state_edges, conflicts = _state_relations(operations, order)
    out_edges.extend(state_edges)
    out_edges = _sorted_unique(out_edges, order)

    cycle = _find_cycle(out_edges, ids)
    if cycle:
        unsupported.append(
            {"operation": cycle[0], "reason": "dependency cycle: " + " -> ".join(cycle)}
        )
    for conflict in conflicts:
        unsupported.append(
            {
                "operation": conflict["operations"][1],
                "reason": f"conflicting writes to {conflict['key']!r} with no declared order",
            }
        )

    return {
        "schema": IR_SCHEMA,
        "input_schema": input_schema,
        "run_id": run_id,
        "dependency_mode": dependency_mode or "none",
        "operations": operations,
        "edges": out_edges,
        "conflicts": conflicts,
        "unsupported": sorted(
            unsupported, key=lambda item: (order.get(item["operation"], len(order)), item["reason"])
        ),
        "summary": _summary(operations, out_edges),
    }


def _declared_dependencies(
    nodes: list[dict[str, Any]], known: set[str], unsupported: list[dict[str, Any]]
) -> tuple[set[str], list[dict[str, Any]]]:
    """Plain ``depends_on`` metadata and ``agentloop.stage`` declarations as data edges."""
    targets: set[str] = set()
    edges: list[dict[str, Any]] = []
    for node in nodes:
        metadata = node["metadata"] if isinstance(node["metadata"], Mapping) else {}
        declared = None
        if "depends_on" in metadata:
            value = metadata["depends_on"]
            if isinstance(value, list) and all(isinstance(item, str) and item for item in value):
                declared = value
            else:
                unsupported.append(
                    {
                        "operation": node["node_id"],
                        "reason": "depends_on must be a list of span ids",
                    }
                )
        stage = metadata.get(STAGE_KEY)
        if isinstance(stage, Mapping) and isinstance(stage.get("depends_on"), list):
            stage_deps = [item for item in stage["depends_on"] if isinstance(item, str) and item]
            if declared is not None and set(declared) != set(stage_deps):
                unsupported.append(
                    {
                        "operation": node["node_id"],
                        "reason": "depends_on and the stage declaration disagree",
                    }
                )
            declared = sorted({*(declared or []), *stage_deps})
        if declared is None:
            continue
        targets.add(node["node_id"])
        for source in declared:
            if source not in known:
                unsupported.append(
                    {
                        "operation": node["node_id"],
                        "reason": f"declared dependency {source} is not in this trace",
                    }
                )
                continue
            edges.append({"source": source, "target": node["node_id"], "kind": "dependency"})
    return targets, edges


def _state_relations(
    operations: list[dict[str, Any]], order: Mapping[str, int]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Declared state edges in recorded order, and write/write pairs with no declared order."""
    readers: dict[str, list[str]] = {}
    writers: dict[str, list[str]] = {}
    for operation in operations:
        for key in operation["reads"]["value"] or []:
            readers.setdefault(key, []).append(operation["id"])
        for key in operation["writes"]["value"] or []:
            writers.setdefault(key, []).append(operation["id"])
    edges: list[dict[str, Any]] = []
    for key, writing in writers.items():
        for writer in writing:
            for other in [*readers.get(key, []), *writing]:
                if other != writer and order[writer] < order[other]:
                    edges.append(
                        {
                            "source": writer,
                            "target": other,
                            "kind": "state",
                            "basis": "declared",
                            "key": key,
                        }
                    )
    conflicts = []
    for key, writing in sorted(writers.items()):
        unique = sorted(set(writing), key=order.__getitem__)
        for index, first in enumerate(unique):
            for second in unique[index + 1 :]:
                conflicts.append({"key": key, "operations": [first, second]})
    return edges, conflicts


def _sorted_unique(edges: list[dict[str, Any]], order: Mapping[str, int]) -> list[dict[str, Any]]:
    seen = set()
    result = []
    for edge in sorted(
        edges,
        key=lambda e: (order[e["source"]], order[e["target"]], e["kind"], e.get("key", "")),
    ):
        identity = (edge["source"], edge["target"], edge["kind"], edge.get("key"))
        if identity not in seen:
            seen.add(identity)
            result.append(edge)
    return result


def _find_cycle(edges: list[dict[str, Any]], ids: list[str]) -> list[str] | None:
    successors: dict[str, list[str]] = {node_id: [] for node_id in ids}
    for edge in edges:
        if edge["kind"] in {"data", "contains"}:
            successors.setdefault(edge["source"], []).append(edge["target"])
    state = dict.fromkeys(successors, 0)  # 0 new, 1 on stack, 2 done
    for start in ids:
        if state.get(start):
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        path: list[str] = []
        while stack:
            node, index = stack.pop()
            if index == 0:
                state[node] = 1
                path.append(node)
            children = successors.get(node, [])
            if index < len(children):
                stack.append((node, index + 1))
                child = children[index]
                if state.get(child) == 1:
                    return [*path[path.index(child) :], child]
                if not state.get(child):
                    stack.append((child, 0))
            else:
                state[node] = 2
                path.pop()
    return None


def _summary(operations: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
    bases: dict[str, dict[str, int]] = {}
    for field_name in ("effect", "idempotent", "deterministic", "reads", "writes"):
        counts = dict.fromkeys(BASES, 0)
        for operation in operations:
            counts[operation[field_name]["basis"]] += 1
        bases[field_name] = counts
    edge_counts: dict[str, int] = {}
    for edge in edges:
        label = f"{edge['kind']}/{edge['basis']}"
        edge_counts[label] = edge_counts.get(label, 0) + 1
    return {
        "operations": len(operations),
        "property_basis": bases,
        "edges": dict(sorted(edge_counts.items())),
    }


# Serialization ------------------------------------------------------------------


def ir_to_json(document: Mapping[str, Any]) -> str:
    """Canonical JSON: sorted keys, no extra whitespace, no NaN."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False)


def ir_digest(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(ir_to_json(document).encode("utf-8")).hexdigest()


def load_execution_ir(document: Mapping[str, Any]) -> dict[str, Any]:
    """Accept a stored IR or a legacy report graph; refuse unknown versions."""
    if not isinstance(document, Mapping):
        raise IRVersionError("an execution IR document must be a JSON object")
    schema = document.get("schema")
    if schema is None and "nodes" in document and "edges" in document:
        return ir_from_graph(document, run_id=str(document.get("run_id", "")))
    if schema not in SUPPORTED_SCHEMAS:
        raise IRVersionError(
            f"unsupported execution IR schema {schema!r}; this version reads "
            + ", ".join(SUPPORTED_SCHEMAS)
        )
    return json.loads(ir_to_json(document))


# Eligibility ----------------------------------------------------------------------


def independence(document: Mapping[str, Any], first: str, second: str) -> dict[str, Any]:
    """Whether two operations may be reordered or run concurrently.

    ``independent`` is True only when both effects are declared ``none`` or
    ``read``, neither declares writes, no declared or observed path connects
    them, and the IR has no unsupported entries for either. Inferred sequence
    edges and unknown properties never make operations independent; they make
    the verdict abstain.
    """
    operations = {operation["id"]: operation for operation in document.get("operations", [])}
    reasons: list[str] = []
    if first == second:
        return {"independent": False, "abstained": False, "reasons": ["same operation"]}
    if first not in operations or second not in operations:
        return {"independent": False, "abstained": True, "reasons": ["operation not in IR"]}
    flagged = {item["operation"] for item in document.get("unsupported", [])}
    abstain = False
    for operation_id in (first, second):
        operation = operations[operation_id]
        effect = operation["effect"]
        if operation_id in flagged:
            reasons.append(f"{operation_id} has unsupported or invalid evidence")
            abstain = True
        if effect["basis"] != "declared":
            reasons.append(f"{operation_id} has no declared effect")
            abstain = True
        elif effect["value"] not in {"none", "read"}:
            reasons.append(f"{operation_id} declares effect {effect['value']!r}")
        if operation["writes"]["value"]:
            reasons.append(f"{operation_id} declares state writes")
    if _connected(document.get("edges", []), first, second, {"declared", "observed"}):
        reasons.append("a declared or observed dependency connects them")
    elif _connected(
        document.get("edges", []), first, second, {"inferred"} | {"declared", "observed"}
    ):
        reasons.append("only an inferred sequence orders them; independence is not established")
        abstain = True
    independent = not reasons
    return {
        "independent": independent,
        "abstained": abstain and not independent,
        "reasons": reasons,
    }


def _connected(
    edges: Iterable[Mapping[str, Any]], first: str, second: str, bases: set[str]
) -> bool:
    successors: dict[str, set[str]] = {}
    for edge in edges:
        if edge.get("basis") in bases:
            successors.setdefault(edge["source"], set()).add(edge["target"])

    def reaches(start: str, goal: str) -> bool:
        pending, seen = [start], {start}
        while pending:
            node = pending.pop()
            for child in successors.get(node, ()):
                if child == goal:
                    return True
                if child not in seen:
                    seen.add(child)
                    pending.append(child)
        return False

    return reaches(first, second) or reaches(second, first)


__all__ = [
    "BASES",
    "EFFECTS",
    "EFFECTS_KEY",
    "IRVersionError",
    "IR_SCHEMA",
    "build_execution_ir",
    "independence",
    "ir_digest",
    "ir_from_graph",
    "ir_to_json",
    "load_execution_ir",
]
