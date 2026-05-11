from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "event_type": self.event_type,
            "duration_ms": round(self.duration_ms, 3),
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
class ExecutionGraph:
    nodes: list[ExecutionNode]
    edges: list[ExecutionEdge]

    @classmethod
    def from_trace(cls, trace: Any) -> "ExecutionGraph":
        nodes: list[ExecutionNode] = []
        edges: list[ExecutionEdge] = []
        previous_id: str | None = None
        for index, event in enumerate(trace.events):
            node_id = event.event_id or f"node_{index}"
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
                )
            )
            if event.parent_id:
                edges.append(ExecutionEdge(source=event.parent_id, target=node_id, kind="parent"))
            elif previous_id is not None:
                edges.append(ExecutionEdge(source=previous_id, target=node_id))
            previous_id = node_id
        return cls(nodes=nodes, edges=edges)

    def total_runtime_ms(self) -> float:
        return sum(node.duration_ms for node in self.nodes)

    def critical_path(self) -> CriticalPath:
        # The current SDK records sequential spans. For now the critical path is the observed sequence.
        return CriticalPath([node.node_id for node in self.nodes], self.total_runtime_ms())

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
            "critical_path": self.critical_path().to_dict(),
            "parallelizable_groups": self.parallelizable_groups(),
            "bottlenecks": self.bottlenecks(),
        }
