"""Execution IR with dependency and effect contracts (issue #396)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentloop.events import AgentEvent
from agentloop.execution_ir import (
    EFFECTS_KEY,
    IR_SCHEMA,
    IRVersionError,
    build_execution_ir,
    independence,
    ir_digest,
    ir_from_graph,
    ir_to_json,
    load_execution_ir,
)
from agentloop.graph import ExecutionGraph
from agentloop.tracer import AgentTrace

GOLDEN = Path(__file__).parent / "fixtures" / "execution_ir"
UPDATE = os.environ.get("AGENTLOOP_UPDATE_GOLDEN") == "1"


def event(
    event_id: str,
    name: str,
    start: int,
    end: int,
    *,
    kind: str = "tool_call",
    parent: str | None = None,
    metadata: dict | None = None,
    status: str = "ok",
    timed: bool = True,
) -> AgentEvent:
    return AgentEvent(
        event_id=event_id,
        run_id="run",
        event_type=kind,
        name=name,
        started_at=f"2026-01-01T00:00:{start:02d}+00:00" if timed else None,
        ended_at=f"2026-01-01T00:00:{end:02d}+00:00" if timed else None,
        duration_ms=float((end - start) * 1000),
        parent_id=parent,
        status=status,
        metadata=metadata or {},
    )


def trace_of(*events: AgentEvent) -> AgentTrace:
    trace = AgentTrace(name="fixture", run_id="run")
    for item in events:
        trace.add_event(item)
    return trace


def effects(**values) -> dict:
    return {EFFECTS_KEY: values}


FIXTURES = {
    "branches": lambda: trace_of(
        event("plan", "plan", 0, 1, kind="model_call", metadata={"depends_on": []}),
        event("left", "search", 1, 3, metadata={"depends_on": ["plan"], **effects(effect="read")}),
        event("right", "fetch", 1, 2, metadata={"depends_on": ["plan"], **effects(effect="read")}),
        event(
            "join", "answer", 3, 4, kind="model_call", metadata={"depends_on": ["left", "right"]}
        ),
    ),
    "loops": lambda: trace_of(
        event("agent", "agent", 0, 9, kind="model_call"),
        *(event(f"step{i}", "step", i * 2, i * 2 + 1, parent="agent") for i in range(1, 4)),
    ),
    "nested": lambda: trace_of(
        event("outer", "outer", 0, 10),
        event("middle", "middle", 1, 9, parent="outer"),
        event("inner", "inner", 2, 3, parent="middle", metadata=effects(effect="none")),
    ),
    "shared_state": lambda: trace_of(
        event(
            "write",
            "save",
            0,
            1,
            metadata={"depends_on": [], **effects(effect="write", writes=["cart"])},
        ),
        event(
            "read",
            "load",
            1,
            2,
            metadata={"depends_on": [], **effects(effect="read", reads=["cart"])},
        ),
        event(
            "other",
            "lookup",
            2,
            3,
            metadata={"depends_on": [], **effects(effect="read", reads=["catalog"])},
        ),
    ),
    "conflicting_writes": lambda: trace_of(
        event("a", "set_price", 0, 1, metadata=effects(effect="write", writes=["price"])),
        event("b", "set_price", 0, 1, metadata=effects(effect="write", writes=["price"])),
    ),
    "missing_evidence": lambda: trace_of(
        event("x", "x", 0, 0, timed=False),
        event("y", "y", 0, 0, timed=False, parent="ghost"),
        event("z", "z", 0, 1, metadata={EFFECTS_KEY: {"effect": "teleport", "idempotent": "yes"}}),
        event("w", "w", 1, 2, metadata={"read_only": True, **effects(effect="write")}),
    ),
}


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_golden_fixtures(name):
    document = build_execution_ir(FIXTURES[name]())
    path = GOLDEN / f"{name}.json"
    rendered = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
    assert rendered == path.read_text(encoding="utf-8"), (
        f"{path.name} changed; rerun with AGENTLOOP_UPDATE_GOLDEN=1 after reviewing"
    )


def test_serialization_is_deterministic_and_order_independent():
    events = list(FIXTURES["branches"]().events)
    forward = build_execution_ir(trace_of(*events))
    backward = build_execution_ir(trace_of(*reversed(events)))
    assert ir_to_json(forward) == ir_to_json(backward)
    assert ir_digest(forward) == ir_digest(backward)
    assert ir_digest(forward) == ir_digest(load_execution_ir(json.loads(ir_to_json(forward))))


def test_properties_name_their_basis():
    document = build_execution_ir(FIXTURES["shared_state"]())
    write, read, other = document["operations"]
    assert write["effect"] == {"value": "write", "basis": "declared", "source": EFFECTS_KEY}
    assert write["writes"]["value"] == ["cart"]
    assert read["idempotent"] == {"value": None, "basis": "unknown"}
    assert read["status"] == {"value": "ok", "basis": "observed"}
    assert other["timing"]["basis"] == "observed"


def test_branches_are_independent_only_with_declared_reads():
    document = build_execution_ir(FIXTURES["branches"]())
    verdict = independence(document, "left", "right")
    assert verdict == {"independent": True, "abstained": False, "reasons": []}
    # plan -> left is a declared data edge.
    assert independence(document, "plan", "left")["independent"] is False


def test_inferred_sequence_never_authorizes():
    # Two read-only root spans in sequence with no declared dependencies.
    trace = trace_of(
        event("a", "search", 0, 1, metadata=effects(effect="read")),
        event("b", "search", 1, 2, metadata=effects(effect="read")),
    )
    document = build_execution_ir(trace)
    assert {(e["kind"], e["basis"]) for e in document["edges"]} == {("sequence", "inferred")}
    verdict = independence(document, "a", "b")
    assert verdict["independent"] is False and verdict["abstained"] is True
    assert "inferred" in verdict["reasons"][0]


def test_undeclared_effects_abstain():
    document = build_execution_ir(FIXTURES["loops"]())
    verdict = independence(document, "step1", "step2")
    assert verdict["independent"] is False and verdict["abstained"] is True
    assert [op["instance"] for op in document["operations"][1:]] == [1, 2, 3]


def test_shared_state_orders_writer_before_reader():
    document = build_execution_ir(FIXTURES["shared_state"]())
    state = [e for e in document["edges"] if e["kind"] == "state"]
    assert state == [
        {"source": "write", "target": "read", "kind": "state", "basis": "declared", "key": "cart"}
    ]
    assert independence(document, "write", "read")["independent"] is False
    assert independence(document, "read", "other")["independent"] is True


def test_conflicting_writes_are_reported_and_block():
    document = build_execution_ir(FIXTURES["conflicting_writes"]())
    assert document["conflicts"] == [{"key": "price", "operations": ["a", "b"]}]
    assert any("conflicting writes" in item["reason"] for item in document["unsupported"])
    assert independence(document, "a", "b")["independent"] is False


def test_nested_spans_are_contained_not_independent():
    document = build_execution_ir(FIXTURES["nested"]())
    assert {(e["source"], e["target"], e["kind"]) for e in document["edges"]} == {
        ("outer", "middle", "contains"),
        ("middle", "inner", "contains"),
    }
    assert independence(document, "outer", "inner")["independent"] is False


def test_missing_and_malformed_evidence_stays_explicit():
    document = build_execution_ir(FIXTURES["missing_evidence"]())
    ops = {op["id"]: op for op in document["operations"]}
    assert ops["x"]["timing"] == {"value": None, "basis": "unknown"}
    reasons = {(item["operation"], item["reason"]) for item in document["unsupported"]}
    assert ("y", "parent span is not in this trace") in reasons
    assert any(op == "z" and "effect must be one of" in r for op, r in reasons)
    assert any(op == "z" and "idempotent must be" in r for op, r in reasons)
    assert any(op == "w" and "contradicts read_only" in r for op, r in reasons)
    assert ops["w"]["effect"]["basis"] == "unknown"
    assert independence(document, "z", "w")["abstained"] is True


def test_cycles_are_unsupported():
    trace = trace_of(
        event("a", "a", 0, 1, metadata={"depends_on": ["b"]}),
        event("b", "b", 1, 2, metadata={"depends_on": ["a"]}),
    )
    document = build_execution_ir(trace)
    assert any(item["reason"].startswith("dependency cycle") for item in document["unsupported"])


def test_legacy_graph_migrates_to_the_same_operations():
    trace = FIXTURES["branches"]()
    legacy = ExecutionGraph.from_trace(trace).to_dict()
    migrated = load_execution_ir(legacy)
    native = build_execution_ir(trace)
    assert migrated["input_schema"] == "agentloop.graph/legacy"
    strip = lambda doc: {k: v for k, v in doc.items() if k not in {"input_schema", "run_id"}}  # noqa: E731
    assert strip(migrated) == strip(native)


def test_legacy_traces_without_declarations_are_all_unknown():
    trace = trace_of(event("a", "a", 0, 1), event("b", "b", 1, 2))
    document = build_execution_ir(trace)
    summary = document["summary"]["property_basis"]
    assert all(counts["unknown"] == 2 for counts in summary.values())


@pytest.mark.parametrize(
    "document",
    [{"schema": "agentloop.ir/v2"}, {"schema": None}, {"nodes": "x", "edges": []}, []],
)
def test_unknown_versions_and_shapes_are_refused(document):
    with pytest.raises(IRVersionError):
        load_execution_ir(document)


def test_ir_from_graph_rejects_bad_nodes():
    with pytest.raises(IRVersionError):
        ir_from_graph({"nodes": [{"name": "no id"}], "edges": []})


def test_schema_constant():
    assert build_execution_ir(trace_of())["schema"] == IR_SCHEMA


def test_independence_edge_cases():
    document = build_execution_ir(FIXTURES["branches"]())
    assert independence(document, "left", "left")["reasons"] == ["same operation"]
    assert independence(document, "left", "nope")["abstained"] is True


def test_dependency_declarations_distinguish_empty_from_missing():
    document = build_execution_ir(FIXTURES["branches"]())
    ops = {op["id"]: op for op in document["operations"]}
    assert ops["plan"]["depends_on"] == {"value": [], "basis": "declared", "source": "depends_on"}
    assert ops["join"]["depends_on"]["value"] == ["left", "right"]
    loops = build_execution_ir(FIXTURES["loops"]())
    assert all(
        op["depends_on"] == {"value": None, "basis": "unknown"} for op in loops["operations"]
    )
