from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentloop.timing import (
    cumulative_span_time_ms,
    elapsed_runtime_ms,
    event_interval_ms,
)
from agentloop.timing import (
    duration_ms as recorded_duration_ms,
)


@dataclass
class ExecutionNode:
    node_id: str
    name: str
    event_type: str
    duration_ms: float
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    status: str = "ok"
    metadata: dict[str, Any] = field(default_factory=dict)
    started_at: str | None = None
    ended_at: str | None = None
    parent_id: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "event_type": self.event_type,
            "duration_ms": round(self.duration_ms, 3),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "parent_id": self.parent_id,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "status": self.status,
            "metadata": self.metadata,
        }


@dataclass
class ExecutionEdge:
    source: str
    target: str
    kind: str = "sequence"

    def to_dict(self) -> dict[str, str]:
        return {"source": self.source, "target": self.target, "kind": self.kind}


@dataclass
class CriticalPath:
    node_ids: list[str]
    duration_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {"node_ids": self.node_ids, "duration_ms": round(self.duration_ms, 3)}


@dataclass
class _PathState:
    node_ids: list[str]
    duration_ms: float
    started_ms: float | None = None
    ended_ms: float | None = None


@dataclass
class ExecutionGraph:
    nodes: list[ExecutionNode]
    edges: list[ExecutionEdge]
    elapsed_ms: float | None = None

    @classmethod
    def from_trace(cls, trace: Any) -> "ExecutionGraph":
        indexed_events = list(enumerate(trace.events))
        indexed_events.sort(key=_event_sort_key)

        nodes: list[ExecutionNode] = []
        event_by_node_id: dict[str, Any] = {}
        for original_index, event in indexed_events:
            node_id = event.event_id or f"node_{original_index}"
            nodes.append(
                ExecutionNode(
                    node_id=node_id,
                    name=event.name,
                    event_type=event.event_type,
                    duration_ms=event.duration_ms,
                    model=event.model,
                    input_tokens=event.input_tokens,
                    output_tokens=event.output_tokens,
                    status=event.status,
                    metadata=event.metadata or {},
                    started_at=event.started_at,
                    ended_at=event.ended_at,
                    parent_id=event.parent_id,
                )
            )
            event_by_node_id[node_id] = event

        node_ids = set(event_by_node_id)
        edges: list[ExecutionEdge] = []
        roots: list[ExecutionNode] = []
        for node in nodes:
            if node.parent_id and node.parent_id in node_ids and node.parent_id != node.node_id:
                edges.append(
                    ExecutionEdge(source=node.parent_id, target=node.node_id, kind="parent")
                )
            else:
                roots.append(node)

        edges.extend(_inferred_sequence_edges(roots))
        return cls(
            nodes=nodes, edges=_deduplicate_edges(edges), elapsed_ms=elapsed_runtime_ms(trace)
        )

    def total_runtime_ms(self) -> float:
        if self.elapsed_ms is not None:
            return max(0.0, self.elapsed_ms)
        intervals = [event_interval_ms(node) for node in self.nodes]
        if intervals and all(interval is not None for interval in intervals):
            valid = [interval for interval in intervals if interval is not None]
            return max(ended for _, ended in valid) - min(started for started, _ in valid)
        return cumulative_span_time_ms(self.nodes)

    def critical_path(self) -> CriticalPath:
        if not self.nodes:
            return CriticalPath([], 0.0)

        node_by_id = {node.node_id: node for node in self.nodes}
        predecessors: dict[str, list[str]] = {node_id: [] for node_id in node_by_id}
        successors: dict[str, list[str]] = {node_id: [] for node_id in node_by_id}
        for edge in self.edges:
            if edge.source not in node_by_id or edge.target not in node_by_id:
                continue
            predecessors[edge.target].append(edge.source)
            successors[edge.source].append(edge.target)

        ordered_ids = _topological_order(self.nodes, predecessors, successors)
        states: dict[str, _PathState] = {}
        for node_id in ordered_ids:
            node = node_by_id[node_id]
            interval = event_interval_ms(node)
            if interval is None:
                own = _PathState([node_id], recorded_duration_ms(node))
            else:
                own = _PathState([node_id], interval[1] - interval[0], interval[0], interval[1])

            candidates = [own]
            for predecessor_id in predecessors[node_id]:
                predecessor = states.get(predecessor_id)
                if predecessor is not None:
                    candidates.append(_extend_path(predecessor, node, interval))
            states[node_id] = max(
                candidates, key=lambda state: (state.duration_ms, len(state.node_ids))
            )

        best = max(states.values(), key=lambda state: (state.duration_ms, len(state.node_ids)))
        return CriticalPath(best.node_ids, best.duration_ms)

    def parallelizable_groups(self) -> list[dict[str, Any]]:
        groups: dict[tuple[str, str], list[ExecutionNode]] = {}
        for node in self.nodes:
            key = (node.event_type, node.name)
            groups.setdefault(key, []).append(node)

        out: list[dict[str, Any]] = []
        for (event_type, name), items in groups.items():
            if event_type != "tool_call" or len(items) < 3:
                continue
            sequential = sum(item.duration_ms for item in items)
            parallel = max(item.duration_ms for item in items)
            out.append(
                {
                    "name": name,
                    "event_type": event_type,
                    "count": len(items),
                    "node_ids": [item.node_id for item in items],
                    "sequential_time_ms": round(sequential, 3),
                    "estimated_parallel_time_ms": round(parallel, 3),
                    "estimated_savings_ms": round(max(0.0, sequential - parallel), 3),
                }
            )
        return sorted(out, key=lambda item: item["estimated_savings_ms"], reverse=True)

    def bottlenecks(self, limit: int = 5) -> list[dict[str, Any]]:
        total = self.total_runtime_ms() or 1.0
        ranked = sorted(self.nodes, key=lambda node: node.duration_ms, reverse=True)[:limit]
        return [
            {
                **node.to_dict(),
                "runtime_share": round(node.duration_ms / total, 4),
            }
            for node in ranked
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "total_runtime_ms": round(self.total_runtime_ms(), 3),
            "cumulative_span_time_ms": round(cumulative_span_time_ms(self.nodes), 3),
            "critical_path": self.critical_path().to_dict(),
            "parallelizable_groups": self.parallelizable_groups(),
            "bottlenecks": self.bottlenecks(),
        }


def _event_sort_key(item: tuple[int, Any]) -> tuple[Any, ...]:
    original_index, event = item
    interval = event_interval_ms(event)
    if interval is None:
        return (1, original_index)
    return (0, interval[0], interval[1], str(event.event_id), original_index)


def _node_sort_key(node: ExecutionNode) -> tuple[Any, ...]:
    interval = event_interval_ms(node)
    if interval is None:
        return (1, node.node_id)
    return (0, interval[0], interval[1], node.node_id)


def _inferred_sequence_edges(roots: list[ExecutionNode]) -> list[ExecutionEdge]:
    if len(roots) < 2:
        return []

    intervals = {node.node_id: event_interval_ms(node) for node in roots}
    if any(interval is None for interval in intervals.values()):
        return [
            ExecutionEdge(source=previous.node_id, target=current.node_id)
            for previous, current in zip(roots, roots[1:])
        ]

    ordered = sorted(roots, key=_node_sort_key)
    edges: list[ExecutionEdge] = []
    for index, current in enumerate(ordered):
        current_interval = intervals[current.node_id]
        if current_interval is None:
            continue
        candidates = []
        for previous in ordered[:index]:
            previous_interval = intervals[previous.node_id]
            if previous_interval is not None and previous_interval[1] <= current_interval[0]:
                candidates.append((previous_interval[1], previous_interval[0], previous))
        if candidates:
            predecessor = max(candidates, key=lambda item: (item[0], item[1], item[2].node_id))[2]
            edges.append(ExecutionEdge(source=predecessor.node_id, target=current.node_id))
    return edges


def _deduplicate_edges(edges: list[ExecutionEdge]) -> list[ExecutionEdge]:
    seen: set[tuple[str, str, str]] = set()
    result: list[ExecutionEdge] = []
    for edge in edges:
        key = (edge.source, edge.target, edge.kind)
        if key not in seen:
            result.append(edge)
            seen.add(key)
    return result


def _topological_order(
    nodes: list[ExecutionNode],
    predecessors: dict[str, list[str]],
    successors: dict[str, list[str]],
) -> list[str]:
    node_by_id = {node.node_id: node for node in nodes}
    indegree = {node_id: len(set(items)) for node_id, items in predecessors.items()}
    ready = sorted(
        (node_by_id[node_id] for node_id, count in indegree.items() if count == 0),
        key=_node_sort_key,
    )
    ordered: list[str] = []
    while ready:
        node = ready.pop(0)
        ordered.append(node.node_id)
        for target in successors[node.node_id]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(node_by_id[target])
                ready.sort(key=_node_sort_key)

    if len(ordered) != len(nodes):
        remaining = [node for node in nodes if node.node_id not in set(ordered)]
        ordered.extend(node.node_id for node in sorted(remaining, key=_node_sort_key))
    return ordered


def _extend_path(
    predecessor: _PathState,
    node: ExecutionNode,
    interval: tuple[float, float] | None,
) -> _PathState:
    node_ids = [*predecessor.node_ids, node.node_id]
    if predecessor.started_ms is not None and predecessor.ended_ms is not None and interval:
        started = min(predecessor.started_ms, interval[0])
        ended = max(predecessor.ended_ms, interval[1])
        return _PathState(node_ids, ended - started, started, ended)
    return _PathState(node_ids, predecessor.duration_ms + recorded_duration_ms(node))
