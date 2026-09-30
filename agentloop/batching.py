"""Model-call groups that do not contradict recorded causal dependencies."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping

from agentloop.workflow_types import stage_summary


def model_batch_groups(graph):
    """Return hypotheses; absent dependencies never prove output independence.

    Index dependencies once, reject unresolved/cyclic ancestry, then inspect each
    candidate group's ancestor closure without recursion or all-pairs searches.
    Inferred sequence edges describe ordering, not causal dependencies.
    """
    by_id = {node.node_id: node for node in graph.nodes}
    if len(by_id) != len(graph.nodes):
        return []
    dependencies = {identity: set() for identity in by_id}
    invalid = set()
    groups = {}
    for node in graph.nodes:
        metadata = node.metadata
        if not isinstance(metadata, Mapping):
            invalid.add(node.node_id)
            metadata = {}
        required = metadata.get("depends_on", [])
        if not isinstance(required, list) or any(
            not isinstance(identity, str) or not identity for identity in required
        ):
            invalid.add(node.node_id)
        else:
            dependencies[node.node_id].update(required)
        stage = stage_summary(node)
        if stage is not None:
            if (
                stage.get("schema_status") != "supported"
                or stage.get("dependency_status") == "invalid"
            ):
                invalid.add(node.node_id)
            elif stage.get("dependencies_declared"):
                dependencies[node.node_id].update(stage["depends_on"])
        if node.parent_id is not None:
            dependencies[node.node_id].add(node.parent_id)
        if node.operation_kind == "model":
            groups.setdefault((node.name, node.model, node.parent_id), []).append(node.node_id)
    if not any(len(identities) >= 3 for identities in groups.values()):
        return []
    for edge in graph.edges:
        if edge.kind in {"dependency", "depends_on", "parent"} and edge.target in by_id:
            dependencies[edge.target].add(edge.source)

    # Removing roots leaves cycles and their descendants unresolved. Invalid or
    # missing ancestors propagate to descendants as the valid portion is visited.
    successors = {identity: [] for identity in by_id}
    indegrees = {}
    for identity, required in dependencies.items():
        indegrees[identity] = len(required)
        for ancestor in required:
            if ancestor in successors:
                successors[ancestor].append(identity)
            else:
                invalid.add(identity)
    ready = deque(identity for identity, count in indegrees.items() if not count)
    visited = set()
    while ready:
        identity = ready.popleft()
        visited.add(identity)
        for successor in successors[identity]:
            if identity in invalid:
                invalid.add(successor)
            indegrees[successor] -= 1
            if indegrees[successor] == 0:
                ready.append(successor)
    invalid.update(by_id.keys() - visited)

    eligible = []
    for (name, _, _), identities in groups.items():
        members = set(identities)
        if len(members) < 3 or members & invalid:
            continue
        pending = [ancestor for identity in identities for ancestor in dependencies[identity]]
        ancestors = set()
        while pending:
            ancestor = pending.pop()
            if ancestor in members:
                break
            if ancestor not in ancestors:
                ancestors.add(ancestor)
                pending.extend(dependencies[ancestor])
        else:
            eligible.append((name, [by_id[identity] for identity in identities]))
    return eligible
