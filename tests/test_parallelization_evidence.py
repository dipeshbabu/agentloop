from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentloop.events import AgentEvent
from agentloop.findings import build_diagnosis, diagnosis_to_markdown
from agentloop.graph import ExecutionEdge, ExecutionGraph
from agentloop.optimizer import build_optimization_plan
from agentloop.plan_export import export_optimization_markdown
from agentloop.tracer import AgentTrace


def _trace(intervals=None, metadata=None):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    intervals = intervals or [(0, 100), (100, 200), (200, 300)]
    trace = AgentTrace(
        name="parallel-evidence",
        started_at=start.isoformat(),
        ended_at=(start + timedelta(milliseconds=300)).isoformat(),
    )
    for index, (begin, end) in enumerate(intervals):
        trace.add_event(
            AgentEvent(
                event_id=f"tool-{index}",
                run_id=trace.run_id,
                name="search",
                event_type="tool_call",
                started_at=(start + timedelta(milliseconds=begin)).isoformat(),
                ended_at=(start + timedelta(milliseconds=end)).isoformat(),
                duration_ms=end - begin,
                metadata=(metadata or [{} for _ in intervals])[index],
            )
        )
    return trace


def test_repeated_names_are_inferred_candidates_with_explicit_assumptions():
    trace = _trace()
    report = trace.report()
    group = report["parallelism_opportunities"][0]
    card = build_optimization_plan(trace)["optimization_cards"][0]
    finding = build_diagnosis(trace)["findings"][0]
    for item in (group, card, finding):
        assert item["evidence_level"] == "inferred"
        assert item["confidence"] == "low"
        assert "No call consumes another call's output." in item["assumptions"]
        assert "Calls do not conflict through shared mutable state." in item["assumptions"]
        assert item["observations"]["timing"] == "non_overlapping"
    assert group["estimated_savings_ms"] == 200
    assert "serial and independent" not in card["why"]
    assert "appear independent" not in str(report["recommendations"])


def test_declared_concurrency_safety_is_distinct_from_verified_evidence():
    group = _trace(metadata=[{"parallel_safe": True}] * 3).report()["parallelism_opportunities"][0]
    assert group["evidence_level"] == "declared"
    assert group["confidence"] == "medium"
    assert group["assumptions"]


@pytest.mark.parametrize(
    "metadata",
    [
        [{}, {}, {"depends_on": ["tool-0"]}],
        [
            {"parallel_safe": True},
            {"parallel_safe": True},
            {"parallel_safe": True, "depends_on": ["tool-0"]},
        ],
        [{}, {}, {"parallel_safe": False}],
        [{}, {}, {"depends_on": "tool-0"}],
        [{}, {}, {"depends_on": ["missing"]}],
    ],
)
def test_dependency_evidence_blocks_unsafe_parallelization(metadata):
    trace = _trace(metadata=metadata)
    assert trace.report()["parallelism_opportunities"] == []
    assert ExecutionGraph.from_trace(trace).parallelizable_groups() == []
    assert not any(
        card["type"] == "parallelize_tools"
        for card in build_optimization_plan(trace)["optimization_cards"]
    )


def test_transitive_dependencies_through_other_event_types_are_respected():
    trace = _trace(metadata=[{}, {}, {"depends_on": ["bridge"]}])
    trace.add_event(
        AgentEvent(
            event_id="bridge",
            run_id=trace.run_id,
            event_type="model_call",
            name="bridge",
            started_at=trace.started_at,
            ended_at=trace.started_at,
            duration_ms=0,
            metadata={"depends_on": ["tool-0"]},
        )
    )
    assert trace.report()["parallelism_opportunities"] == []
    assert ExecutionGraph.from_trace(trace).parallelizable_groups() == []


def test_graph_dependency_edges_are_not_confused_with_inferred_sequence_edges():
    graph = ExecutionGraph.from_trace(_trace())
    assert graph.parallelizable_groups()
    graph.edges.append(ExecutionEdge("tool-0", "tool-2", "dependency"))
    assert graph.parallelizable_groups() == []


@pytest.mark.parametrize(
    "intervals",
    [
        [(0, 100), (0, 100), (0, 100)],
        [(0, 100), (50, 150), (150, 250)],
    ],
)
def test_already_overlapping_calls_do_not_claim_serial_savings(intervals):
    trace = _trace(intervals)
    assert trace.report()["parallelism_opportunities"] == []
    assert ExecutionGraph.from_trace(trace).parallelizable_groups() == []


def test_legacy_timing_keeps_a_low_confidence_candidate():
    trace = _trace()
    for event in trace.events:
        event.ended_at = event.started_at
    group = trace.report()["parallelism_opportunities"][0]
    assert group["confidence"] == "low"
    assert group["observations"]["timing"] == "unavailable"
    assert "Calls ran serially; timing is incomplete or inconsistent." in group["assumptions"]


def test_parallelization_assumptions_survive_markdown_and_persistence(tmp_path):
    from agentloop.store import SQLiteTraceStore

    trace = _trace()
    plan = build_optimization_plan(trace)
    diagnosis = build_diagnosis(trace)
    output = export_optimization_markdown(plan, tmp_path / "plan.md").read_text(encoding="utf-8")
    for markdown in (output, diagnosis_to_markdown(diagnosis)):
        assert "Evidence level: inferred" in markdown
        assert "shared mutable state" in markdown
        assert "assuming" in markdown
    store = SQLiteTraceStore(path=str(tmp_path / "trace.db"))
    store.save_trace(trace)
    saved = store.list_findings()[0]
    assert saved["finding"]["evidence_level"] == "inferred"
    assert saved["finding"]["assumptions"] == diagnosis["findings"][0]["assumptions"]
    assert saved["finding"]["observations"] == diagnosis["findings"][0]["observations"]


def test_partial_declarations_do_not_upgrade_the_group():
    trace = _trace(metadata=[{"parallel_safe": True}, {}, {}])
    assert trace.report()["parallelism_opportunities"][0]["evidence_level"] == "inferred"


def test_evidence_qualification_preserves_finding_identity_for_the_same_spans():
    trace = _trace()
    inferred = build_diagnosis(trace)["findings"][0]
    for event in trace.events:
        event.metadata["parallel_safe"] = True
    declared = build_diagnosis(trace)["findings"][0]
    assert inferred["evidence_level"] != declared["evidence_level"]
    assert inferred["finding_id"] == declared["finding_id"]


def test_common_external_input_dependency_does_not_imply_inter_call_dependency():
    trace = _trace(metadata=[{"parallel_safe": True, "depends_on": ["input"]}] * 3)
    trace.add_event(
        AgentEvent(
            event_id="input",
            run_id=trace.run_id,
            event_type="model_call",
            name="input",
            started_at=trace.started_at,
            ended_at=trace.started_at,
            duration_ms=0,
        )
    )
    assert trace.report()["parallelism_opportunities"][0]["evidence_level"] == "declared"


def test_tools_from_different_parent_scopes_are_not_combined():
    trace = _trace()
    trace.events[0].parent_id = "another-scope"
    assert trace.report()["parallelism_opportunities"] == []


@pytest.mark.parametrize("declaration", ["true", 1, None])
def test_malformed_concurrency_declarations_are_not_trusted(declaration):
    assert (
        _trace(metadata=[{"parallel_safe": declaration}] * 3).report()["parallelism_opportunities"]
        == []
    )


@pytest.fixture
def isolated_dashboard_cache():
    import streamlit as st

    # load_store() is cached across AppTest instances. Each test must use its
    # own database rather than whichever fixture populated the cache first.
    st.cache_resource.clear()
    yield
    st.cache_resource.clear()


@pytest.mark.parametrize("page", ["Optimization", "Diagnosis"])
def test_dashboard_shows_parallelization_evidence_and_assumptions(
    tmp_path, monkeypatch, page, isolated_dashboard_cache
):
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    from agentloop.store import SQLiteTraceStore

    database = tmp_path / "dashboard.db"
    monkeypatch.setenv("AGENTLOOP_SQLITE_PATH", str(database))
    SQLiteTraceStore(path=str(database)).save_trace(_trace())
    app = AppTest.from_file(
        str(Path(__file__).resolve().parents[1] / "dashboard" / "app.py"), default_timeout=60
    )
    app.run()
    app.sidebar.radio[0].set_value(page).run()
    assert not app.exception
    assert any("Evidence level: inferred" in item.value for item in app.markdown)
    assert any("shared mutable state" in item.value for item in app.markdown)
    assert any("uncalibrated; predicted savings" in item.value for item in app.markdown)
