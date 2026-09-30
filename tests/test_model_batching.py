from __future__ import annotations

from copy import deepcopy

import pytest

from agentloop.batching import model_batch_groups
from agentloop.events import AgentEvent
from agentloop.findings import build_diagnosis
from agentloop.graph import ExecutionEdge, ExecutionGraph
from agentloop.optimizer import build_optimization_plan
from agentloop.rules import AnalysisContext, run_rules
from agentloop.tracer import AgentTrace
from agentloop.workflow_types import operation_metadata


def _trace():
    trace = AgentTrace("batching", elapsed_ms=3000)
    for index in range(3):
        trace.add_event(
            AgentEvent(
                event_id=f"model-{index}",
                run_id=trace.run_id,
                event_type="model_call",
                name="classify",
                model="gpt-4.1-mini",
                started_at=f"2026-01-01T00:00:0{index}+00:00",
                ended_at=f"2026-01-01T00:00:0{index + 1}+00:00",
                duration_ms=1000,
                input_tokens=100,
                output_tokens=10,
                token_provenance="provider",
            )
        )
    return trace


def _bridge(trace, identity, required):
    event = deepcopy(trace.events[0])
    event.event_id = identity
    event.event_type = "tool_call"
    event.name = identity
    event.metadata = {"depends_on": required}
    trace.add_event(event)
    return event


def _batch_cards(trace):
    return [
        card for card in trace.report()["finding_candidates"] if card["type"] == "batch_model_calls"
    ]


@pytest.mark.parametrize("required", [["model-0"], ["model-2"], ["missing"], "model-0", [None]])
def test_dependent_or_invalid_groups_are_absent_from_all_report_surfaces(required):
    trace = _trace()
    trace.events[2].metadata["depends_on"] = required
    report = trace.report()
    assert report["analysis_complete"] is True
    for cards in (
        report["finding_candidates"],
        build_optimization_plan(trace, report)["optimization_cards"],
        build_diagnosis(trace)["findings"],
    ):
        assert not any(card["type"] == "batch_model_calls" for card in cards)


def test_transitive_dependency_through_tool_is_not_batchable():
    trace = _trace()
    _bridge(trace, "lookup", ["model-0"])
    trace.events[2].metadata["depends_on"] = ["lookup"]
    assert _batch_cards(trace) == []


@pytest.mark.parametrize("failure", ["missing", "malformed", "cycle"])
def test_invalid_ancestor_dependency_blocks_batching(failure):
    trace = _trace()
    bridge = _bridge(trace, "lookup", ["missing"])
    if failure == "malformed":
        bridge.metadata["depends_on"] = 1
    elif failure == "cycle":
        bridge.metadata["depends_on"] = ["other"]
        _bridge(trace, "other", ["lookup"])
    trace.events[2].metadata["depends_on"] = ["lookup"]
    assert _batch_cards(trace) == []


def test_common_input_and_parent_do_not_imply_inter_call_dependency():
    trace = _trace()
    parent = _bridge(trace, "input", [])
    for event in trace.events[:3]:
        event.metadata["depends_on"] = [parent.event_id]
        event.parent_id = parent.event_id
    assert len(_batch_cards(trace)) == 1


@pytest.mark.parametrize("field,value", [("model", "other-model"), ("parent_id", "other-parent")])
def test_different_models_or_parent_scopes_are_not_combined(field, value):
    trace = _trace()
    setattr(trace.events[2], field, value)
    assert _batch_cards(trace) == []


def test_stage_only_dependencies_are_respected():
    trace = _trace()
    trace.events[2].metadata = operation_metadata("model", depends_on=["model-0"])
    del trace.events[2].metadata["depends_on"]
    assert _batch_cards(trace) == []


@pytest.mark.parametrize("kind", ["dependency", "depends_on", "parent"])
def test_explicit_graph_edges_override_inferred_sequence(kind):
    trace = _trace()
    graph = ExecutionGraph.from_trace(trace)
    context = AnalysisContext(trace.report(), graph)
    assert any(card.type.value == "batch_model_calls" for card in run_rules(context)[0])
    graph.edges.append(ExecutionEdge("model-0", "model-2", kind))
    assert not any(card.type.value == "batch_model_calls" for card in run_rules(context)[0])


def test_invalid_group_does_not_hide_an_independent_group():
    trace = _trace()
    independent = []
    for event in list(trace.events):
        duplicate = deepcopy(event)
        duplicate.event_id += "-independent"
        duplicate.name = "independent"
        independent.append(duplicate.event_id)
        trace.add_event(duplicate)
    trace.events[2].metadata["depends_on"] = ["model-0"]
    cards = _batch_cards(trace)
    assert len(cards) == 1
    assert cards[0]["affected_nodes"] == independent


def test_duplicate_span_identity_cannot_support_batching():
    trace = _trace()
    trace.events[2].event_id = trace.events[0].event_id
    assert _batch_cards(trace) == []


def test_missing_common_parent_does_not_establish_a_batchable_scope():
    trace = _trace()
    for event in trace.events:
        event.parent_id = "missing-parent"
    assert _batch_cards(trace) == []


@pytest.mark.parametrize(
    "metadata",
    [
        {"agentloop.stage": {"schema_version": "999.0"}},
        operation_metadata("model", depends_on=["model-0"]) | {"depends_on": []},
    ],
)
def test_unsupported_or_conflicting_stage_evidence_blocks_batching(metadata):
    trace = _trace()
    trace.events[2].metadata = metadata
    assert _batch_cards(trace) == []


def test_deep_shared_input_chain_is_checked_without_recursion():
    trace = _trace()
    previous = None
    for index in range(1500):
        identity = f"input-{index}"
        _bridge(trace, identity, [] if previous is None else [previous])
        previous = identity
    for event in trace.events[:3]:
        event.metadata["depends_on"] = [previous]
    groups = model_batch_groups(ExecutionGraph.from_trace(trace))
    assert len(groups) == 1
    assert [node.node_id for node in groups[0][1]] == ["model-0", "model-1", "model-2"]
