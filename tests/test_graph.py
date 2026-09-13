from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from agentloop.events import AgentEvent
from agentloop.graph import CriticalPath, ExecutionEdge, ExecutionGraph, ExecutionNode
from agentloop.tracer import AgentTrace

_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _timestamp(offset_ms: int) -> str:
    return (_BASE + timedelta(milliseconds=offset_ms)).isoformat()


def _trace(spans: list[tuple[str, int, int]]) -> AgentTrace:
    trace = AgentTrace("graph", run_id="graph-test", elapsed_ms=100)
    for node_id, started, ended in spans:
        trace.add_event(
            AgentEvent(
                event_id=node_id,
                run_id=trace.run_id,
                event_type="tool_call",
                name=node_id,
                started_at=_timestamp(started),
                ended_at=_timestamp(ended),
                duration_ms=ended - started,
            )
        )
    return trace


def _node(node_id: str, duration_ms: float = 1) -> ExecutionNode:
    return ExecutionNode(node_id, node_id, "tool_call", duration_ms)


def test_empty_graph_has_no_critical_path() -> None:
    assert ExecutionGraph([], []).critical_path() == CriticalPath([], 0)


@pytest.mark.parametrize("reverse", [False, True])
def test_sequence_edges_select_latest_finished_span_with_stable_ties(reverse: bool) -> None:
    spans = [
        ("a", 0, 10),
        ("long", 0, 50),
        ("b", 2, 10),
        ("c", 2, 10),
        ("overlap", 3, 20),
        ("next", 10, 15),
        ("later", 20, 25),
        ("tail", 50, 60),
    ]
    graph = ExecutionGraph.from_trace(_trace(spans[::-1] if reverse else spans))

    assert graph.edges == [
        ExecutionEdge("c", "next"),
        ExecutionEdge("overlap", "later"),
        ExecutionEdge("long", "tail"),
    ]
    assert graph.critical_path() == CriticalPath(["long", "tail"], 60)


def test_zero_duration_spans_can_follow_each_other_without_self_edges() -> None:
    graph = ExecutionGraph.from_trace(
        _trace([("end", 1, 1), ("work", 0, 1), ("z2", 0, 0), ("z1", 0, 0)])
    )

    assert graph.edges == [
        ExecutionEdge("z1", "z2"),
        ExecutionEdge("z2", "work"),
        ExecutionEdge("work", "end"),
    ]
    assert graph.critical_path() == CriticalPath(["z1", "z2", "work", "end"], 1)


def test_inferred_edges_match_exhaustive_predecessor_selection() -> None:
    rng = random.Random(153)
    for _ in range(50):
        spans = []
        for index in range(30):
            started = rng.randrange(20)
            spans.append((f"node-{index}", started, started + rng.randrange(10)))
        ordered = sorted(spans, key=lambda span: (span[1], span[2], span[0]))
        expected = []
        for index, (node_id, started, _) in enumerate(ordered):
            eligible = [span for span in ordered[:index] if span[2] <= started]
            if eligible:
                predecessor = max(eligible, key=lambda span: (span[2], span[1], span[0]))
                expected.append(ExecutionEdge(predecessor[0], node_id))
        rng.shuffle(spans)

        assert ExecutionGraph.from_trace(_trace(spans)).edges == expected


@pytest.mark.parametrize("ended_at", ["", "invalid", _timestamp(0)])
def test_unreliable_timing_keeps_sequence_fallback(ended_at: str) -> None:
    trace = _trace([("unknown", 0, 100), ("late", 10, 20), ("early", 0, 10)])
    trace.events[0].ended_at = ended_at

    graph = ExecutionGraph.from_trace(trace)

    assert graph.edges == [ExecutionEdge("early", "late"), ExecutionEdge("late", "unknown")]
    assert graph.critical_path() == CriticalPath(["early", "late", "unknown"], 120)


def test_critical_path_preserves_length_and_predecessor_order_ties() -> None:
    graph = ExecutionGraph(
        [_node("root"), _node("b"), _node("a"), _node("sink", 0)],
        [
            ExecutionEdge("root", "a"),
            ExecutionEdge("root", "b"),
            ExecutionEdge("b", "sink"),
            ExecutionEdge("a", "sink"),
        ],
    )

    assert graph.critical_path() == CriticalPath(["root", "b", "sink"], 2)


@pytest.mark.parametrize("duplicate_kind", ["sequence", "parent", "dependency"])
@pytest.mark.parametrize("timestamped", [False, True])
def test_duplicate_edges_wait_for_every_predecessor(duplicate_kind: str, timestamped: bool) -> None:
    if timestamped:
        nodes = ExecutionGraph.from_trace(_trace([("a", 0, 1), ("b", 1, 2), ("z", 2, 12)])).nodes
    else:
        nodes = [_node("a"), _node("b"), _node("z", 10)]
    edges = [ExecutionEdge("a", "b"), ExecutionEdge("z", "b")]
    graph = ExecutionGraph(nodes, edges)
    expected = CriticalPath(["z", "b"], 11)
    assert graph.critical_path() == expected

    # A duplicate can have another kind, such as a parent relationship also
    # declared as a dependency. It must not release b before z is processed.
    edges.insert(1, ExecutionEdge("a", "b", duplicate_kind))
    assert graph.critical_path() == expected
    assert graph.to_dict()["critical_path"] == expected.to_dict()


def test_duplicate_edges_do_not_change_seeded_dag_critical_paths() -> None:
    rng = random.Random(179)
    for _ in range(50):
        nodes = [_node(f"node-{index:02d}", rng.randrange(10)) for index in range(20)]
        rng.shuffle(nodes)
        edges = [
            ExecutionEdge(source.node_id, target.node_id)
            for index, source in enumerate(nodes)
            for target in nodes[index + 1 :]
            if rng.random() < 0.2
        ]
        expected = ExecutionGraph(nodes, edges).critical_path()
        repeated_edges = [
            edge
            for original in edges
            for edge in (
                original,
                ExecutionEdge(original.source, original.target, "dependency"),
                ExecutionEdge(original.source, original.target, "parent"),
            )
        ]
        assert ExecutionGraph(nodes, repeated_edges).critical_path() == expected


def test_wide_fanout_has_deterministic_critical_path() -> None:
    children = [f"child-{index:04d}" for index in range(200)]
    graph = ExecutionGraph(
        [_node(node_id) for node_id in reversed(children)] + [_node("root")],
        [ExecutionEdge("root", node_id) for node_id in reversed(children)],
    )

    assert graph.critical_path() == CriticalPath(["root", children[0]], 2)


@pytest.mark.parametrize("timestamped", [False, True])
def test_deep_critical_path_reconstructs_iteratively(timestamped: bool) -> None:
    node_ids = [f"node-{index:04d}" for index in range(1500)]
    if timestamped:
        # Nested spans share a temporal envelope rather than adding their durations.
        nodes = ExecutionGraph.from_trace(_trace([(node_id, 0, 10) for node_id in node_ids])).nodes
    else:
        nodes = [_node(node_id) for node_id in node_ids]
    graph = ExecutionGraph(
        nodes[::-1],
        [ExecutionEdge(source, target, "parent") for source, target in zip(node_ids, node_ids[1:])],
    )

    assert graph.critical_path() == CriticalPath(node_ids, 10 if timestamped else len(node_ids))


def test_critical_path_keeps_additive_timing_after_an_unreliable_span() -> None:
    graph = ExecutionGraph.from_trace(_trace([("first", 0, 10), ("last", 30, 40)]))
    graph.nodes.insert(1, _node("unknown", 5))
    graph.edges = [ExecutionEdge("first", "unknown"), ExecutionEdge("unknown", "last")]

    assert graph.critical_path() == CriticalPath(["first", "unknown", "last"], 25)


def test_cycle_fallback_and_dangling_edges_remain_deterministic() -> None:
    graph = ExecutionGraph(
        [_node("b"), _node("a"), _node("root")],
        [
            ExecutionEdge("a", "b"),
            ExecutionEdge("b", "a"),
            ExecutionEdge("missing", "root"),
            ExecutionEdge("root", "missing"),
        ],
    )

    assert graph.critical_path() == CriticalPath(["a", "b"], 2)


def test_critical_path_reflects_graph_mutations() -> None:
    graph = ExecutionGraph([_node("a"), _node("b")], [])
    assert graph.critical_path() == CriticalPath(["a"], 1)

    graph.edges.append(ExecutionEdge("a", "b"))
    graph.nodes[1].duration_ms = 4
    assert graph.critical_path() == CriticalPath(["a", "b"], 5)
