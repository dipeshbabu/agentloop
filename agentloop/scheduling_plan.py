"""Stable DAG planning using current declarations, never historical success."""

from __future__ import annotations

import heapq

from agentloop.context_types import fingerprint
from agentloop.scheduling_types import ScheduleConfig, SchedulingError, ToolCall


def plan_tools(calls, config, *, enforce=True):
    if (
        type(config) is not ScheduleConfig
        or not isinstance(calls, (list, tuple))
        or not 1 <= len(calls) <= 256
        or any(type(call) is not ToolCall for call in calls)
    ):
        raise SchedulingError("invalid_plan")
    ordered = tuple(call.call_id for call in calls)
    by_id = {call.call_id: call for call in calls}
    if len(by_id) != len(calls):
        raise SchedulingError("duplicate_call")
    rank = {identity: index for index, identity in enumerate(ordered)}
    dependencies = {call.call_id: set(call.depends_on or ()) for call in calls}
    if sum(map(len, dependencies.values())) > 4096:
        raise SchedulingError("invalid_plan")
    if any(not values <= by_id.keys() for values in dependencies.values()):
        raise SchedulingError("missing_dependency")
    successors, indegrees = {identity: set() for identity in ordered}, {}
    for identity, values in dependencies.items():
        indegrees[identity] = len(values)
        for source in values:
            successors[source].add(identity)
    ready = [rank[identity] for identity in ordered if indegrees[identity] == 0]
    heapq.heapify(ready)
    topological = []
    while ready:
        identity = ordered[heapq.heappop(ready)]
        topological.append(identity)
        for target in successors[identity]:
            indegrees[target] -= 1
            if indegrees[target] == 0:
                heapq.heappush(ready, rank[target])
    if len(topological) != len(calls):
        raise SchedulingError("cycle")
    unknown = [
        call.call_id
        for call in calls
        if call.depends_on is None
        or call.reads is None
        or call.writes is None
        or call.concurrent is None
        or call.effect == "unknown"
    ]
    unsupported = [call.call_id for call in calls if call.concurrent is False]
    if unknown and config.on_unknown == "error" and enforce:
        raise SchedulingError("unknown_safety")
    if any(call.depends_on is None for call in calls):
        # Unknown dependency semantics permit only the caller's original order.
        if any(
            rank[source] >= rank[target]
            for target, sources in dependencies.items()
            for source in sources
        ):
            raise SchedulingError("unknown_order")
        topological = list(ordered)
    effective = {identity: set(values) for identity, values in dependencies.items()}
    conflicts = []
    if unknown or unsupported:
        for previous, following in zip(topological, topological[1:]):
            effective[following].add(previous)
    else:
        access = {
            identity: (set(by_id[identity].reads), set(by_id[identity].writes))
            for identity in ordered
        }
        for index, before in enumerate(topological):
            reads, writes = access[before]
            for after in topological[index + 1 :]:
                other_reads, other_writes = access[after]
                shared = (writes & (other_reads | other_writes)) | (other_writes & reads)
                if shared:
                    effective[after].add(before)
                    conflicts.append(
                        {"before": before, "after": after, "resources": sorted(shared)}
                    )
    declaration = {
        "calls": [call.declaration() for call in calls],
        "configuration": config.to_dict(),
        "serial_order": topological,
        "effective_dependencies": {
            identity: sorted(effective[identity], key=rank.get) for identity in ordered
        },
        "unknown_safety": unknown,
        "unsupported_concurrency": unsupported,
        "resource_conflicts": conflicts,
        "proposed_concurrency": 1 if unknown or unsupported else config.max_concurrency,
    }
    return {**declaration, "plan_hash": fingerprint(declaration)}
