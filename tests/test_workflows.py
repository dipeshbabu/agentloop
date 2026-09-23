from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import FrozenInstanceError

import pytest
from workflow_fixtures import pipeline

from agentloop import (
    AgentTrace,
    ExecutionTrace,
    StageInfo,
    WorkflowInfo,
    operation_metadata,
    record_operation,
    set_workflow_outcome,
    trace_agent,
    trace_operation,
    trace_workflow,
    workflow_metadata,
)
from agentloop.entrypoint import _quickstart_trace
from agentloop.exporters import export_report_markdown
from agentloop.findings import build_diagnosis
from agentloop.graph import ExecutionEdge, ExecutionGraph
from agentloop.html_report import analysis_to_html
from agentloop.optimizer import build_optimization_plan
from agentloop.otel import trace_from_otel, trace_to_otel
from agentloop.replay import build_replay_report
from agentloop.studies import summarize_study
from agentloop.tracer import current_event_id, current_trace, record_model_call
from agentloop.workflow_types import STAGE_KEY, WORKFLOW_KEY, stage_summary


def test_legacy_agent_uses_same_trace_type_without_new_metadata():
    original = {"purpose": "unchanged"}
    with trace_agent("legacy", metadata=original) as trace:
        assert isinstance(trace, ExecutionTrace) and ExecutionTrace is AgentTrace
        assert current_trace() is trace
    assert trace.metadata == original
    assert "execution" not in trace.report()
    assert "execution" not in ExecutionGraph.from_trace(trace).to_dict()


@pytest.mark.parametrize("branching", [False, True])
def test_generic_pipeline_is_not_a_collection_of_tool_or_agent_calls(branching):
    trace = pipeline(branching=branching)
    report = trace.report()
    assert report["tool_call_count"] == report["model_call_count"] == 0
    assert all(event.event_type == "operation" for event in trace.events)
    assert report["operation_counts"]["classifier"] == report["operation_counts"]["rule"] == 1
    assert report["execution"]["workflow_id"] == "mail-router"
    assert report["execution"]["cost_scope"] == "recorded_model_calls"
    graph = ExecutionGraph.from_trace(trace).to_dict()
    assert graph["execution"] == report["execution"]
    assert graph["dependency_evidence"]["valid"]
    assert graph["dependency_evidence"]["all_spans_declared"]
    expected = {("classify", "priority"), ("priority", "route")}
    if branching:
        expected |= {("classify", "lookup"), ("lookup", "route")}
    assert {(edge["source"], edge["target"]) for edge in graph["edges"]} == expected
    assert all(edge["kind"] == "dependency" for edge in graph["edges"])
    assert graph["nodes"][0]["stage"]["input_ref"] == "input:classify"
    assert build_diagnosis(trace)["execution"] == report["execution"]


def test_root_and_span_lifecycles_capture_references_without_raw_bodies():
    private = "private input body never supplied to tracing"
    metadata = {"labels": ["before"]}
    with trace_workflow(
        "workflow", workflow=WorkflowInfo("wf", "1"), task_id=0, metadata=metadata
    ) as trace:
        assert trace.metadata[WORKFLOW_KEY]["status"] == "running"
        metadata["labels"].append("later")
        with trace_operation(
            "classify", kind="classifier", stage=StageInfo("classify", "1"), depends_on=[]
        ) as span:
            assert current_event_id() == span.event_id
            assert private.startswith("private")
            span.set_outcome("classified", output_ref="result:1")
        set_workflow_outcome(trace, "routed", output_ref="artifact:1")
        assert current_event_id() is None
    assert trace.metadata["labels"] == ["before"]
    assert trace.metadata[WORKFLOW_KEY]["status"] == "completed"
    assert trace.metadata[WORKFLOW_KEY]["outcome"] == "routed"
    assert stage_summary(trace.events[0])["outcome"] == "classified"
    assert private not in json.dumps(trace.to_dict())
    with pytest.raises(RuntimeError, match="finished"):
        span.set_outcome("changed")
    assert current_trace() is None and current_event_id() is None


@pytest.mark.parametrize(
    "error,status",
    [
        (ValueError("private failure"), "failed"),
        (asyncio.CancelledError("private cancel"), "cancelled"),
        (GeneratorExit(), "interrupted"),
    ],
)
def test_original_exceptions_cancellation_and_context_are_preserved(error, status):
    with pytest.raises(type(error)) as caught:
        with trace_workflow("failed", workflow=WorkflowInfo("wf", "1")) as trace:
            with trace_operation("work", kind="rule"):
                raise error
    assert caught.value is error
    assert trace.metadata[WORKFLOW_KEY]["status"] == status
    assert trace.events[0].status == "error"
    assert trace.events[0].error is None
    assert trace.events[0].metadata["error_type"] == type(error).__name__
    assert stage_summary(trace.events[0])["execution_status"] == status
    assert "private failure" not in json.dumps(trace.to_dict())
    assert current_trace() is None and current_event_id() is None


def test_error_detail_capture_is_explicit():
    with trace_workflow("failed", workflow=WorkflowInfo("wf", "1")) as trace:
        with pytest.raises(ValueError):
            with trace_operation("work", kind="rule", capture_error_detail=True):
                raise ValueError("explicit detail")
    assert trace.events[0].error == "explicit detail"


def test_async_branches_restore_parents_and_can_join_with_dependencies():
    async def scenario():
        with trace_workflow("async", workflow=WorkflowInfo("wf", "1")) as trace:
            with trace_operation("parent", kind="workflow") as parent:

                async def branch(name):
                    with trace_operation(name, kind="rule", depends_on=[]) as span:
                        await asyncio.sleep(0)
                        assert current_event_id() == span.event_id
                        return span.event_id

                children = await asyncio.gather(branch("a"), branch("b"))
                assert current_event_id() == parent.event_id
                with trace_operation("join", kind="transform", depends_on=children):
                    pass
        nodes = {event.name: event for event in trace.events}
        assert (
            nodes["a"].parent_id
            == nodes["b"].parent_id
            == nodes["join"].parent_id
            == parent.event_id
        )
        assert ExecutionGraph.from_trace(trace).dependency_summary()["valid"]

    asyncio.run(scenario())


def test_explicit_record_target_does_not_inherit_an_unrelated_parent():
    target = pipeline()
    with trace_agent("ambient") as ambient:
        with trace_operation("parent", kind="rule"):
            record_operation(
                "callback",
                kind="external_service",
                duration_ms=1,
                started_at=target.started_at,
                ended_at=target.ended_at,
                trace=target,
                event_id="callback",
            )
            assert current_trace() is ambient
    assert target.events[-1].parent_id is None
    assert len(ambient.events) == 1


def test_physical_model_usage_and_logical_classifier_stage_are_distinct():
    with trace_workflow("model", workflow=WorkflowInfo("wf", "1")) as trace:
        with trace_operation(
            "model",
            kind="model",
            model="gpt-4o-mini",
            input_tokens=10,
            output_tokens=2,
            stage=StageInfo("classify", "1", kind="classifier"),
        ):
            pass
    assert trace.events[0].event_type == "model_call"
    assert trace.events[0].operation_kind == "model"
    assert stage_summary(trace.events[0])["kind"] == "classifier"
    assert trace.report()["model_call_count"] == 1
    assert trace.report()["input_tokens"] == 10
    assert trace.report()["token_status"] == "exact"
    with trace_workflow("unknown usage", workflow=WorkflowInfo("wf", "1")) as unknown:
        with trace_operation("model", kind="model", model="gpt-4o-mini"):
            pass
    assert unknown.report()["token_status"] == "unavailable"


@pytest.mark.parametrize(
    "options",
    [
        {"kind": "rule", "input_tokens": 1},
        {"kind": "model", "input_tokens": True},
        {"kind": "model", "input_tokens": 1, "token_provenance": "provider"},
        {"kind": "model", "token_provenance": ""},
    ],
)
def test_invalid_usage_is_rejected_before_the_body_runs(options):
    with trace_agent("invalid") as trace:
        with pytest.raises(ValueError):
            with trace_operation("body", **options):
                pytest.fail("invalid declaration dispatched work")
    assert trace.events == []


def test_integration_metadata_requires_no_framework_types():
    trace = pipeline()
    metadata = operation_metadata(
        "model", stage=StageInfo("score", "scorer-v1", kind="evaluator"), depends_on=["route"]
    )
    record_model_call(
        "integration callback",
        duration_ms=1,
        started_at=trace.started_at,
        ended_at=trace.ended_at,
        input_tokens=4,
        output_tokens=1,
        metadata=metadata,
        trace=trace,
    )
    assert stage_summary(trace.events[-1])["kind"] == "evaluator"
    assert stage_summary(trace.events[-1])["depends_on"] == ["route"]


def test_unknown_operation_and_stage_kinds_are_preserved_without_tool_inference():
    trace = pipeline()
    record_operation(
        "future",
        kind=" FUTURE_OPERATION ",
        stage=StageInfo("future", "1", kind="FUTURE_STAGE"),
        duration_ms=1,
        started_at=trace.started_at,
        ended_at=trace.ended_at,
        trace=trace,
    )
    loaded = trace_from_otel(trace_to_otel(trace))
    event = loaded.events[-1]
    assert event.event_type == "operation" and event.operation_kind == "unknown"
    assert event.metadata["operation_kind"] == " FUTURE_OPERATION "
    assert stage_summary(event)["kind"] == "unknown"
    assert stage_summary(event)["raw_kind"] == "FUTURE_STAGE"
    assert loaded.report()["tool_call_count"] == 0


def test_generic_otlp_spans_do_not_claim_genai_usage_and_keep_native_fields():
    trace = pipeline(branching=True)
    trace.events[0].model = "local-classifier-v1"
    trace.events[0].input_tokens = 12  # Existing native fields still round-trip.
    payload = trace_to_otel(trace)
    for span in payload["resourceSpans"][0]["scopeSpans"][0]["spans"]:
        assert not any(item["key"].startswith("gen_ai.") for item in span["attributes"])
    loaded = trace_from_otel(payload)
    for _ in range(2):
        loaded = trace_from_otel(trace_to_otel(loaded))
    assert loaded.metadata[WORKFLOW_KEY] == trace.metadata[WORKFLOW_KEY]
    assert loaded.events[0].model == "local-classifier-v1" and loaded.events[0].input_tokens == 12
    assert [stage_summary(event) for event in loaded.events] == [
        stage_summary(event) for event in trace.events
    ]


@pytest.mark.parametrize("problem", ["missing", "cycle", "invalid"])
def test_invalid_dependency_evidence_is_retained_without_a_fabricated_critical_path(problem):
    trace = pipeline()
    if problem == "missing":
        trace.events[-1].metadata[STAGE_KEY]["depends_on"] = ["absent"]
        trace.events[-1].metadata["depends_on"] = ["absent"]
    elif problem == "cycle":
        trace.events[0].metadata[STAGE_KEY]["depends_on"] = ["route"]
        trace.events[0].metadata["depends_on"] = ["route"]
    else:
        trace.events[-1].metadata[STAGE_KEY]["depends_on"] = "invalid"
    graph = ExecutionGraph.from_trace(trace)
    assert not graph.dependency_summary()["valid"]
    assert graph.to_dict()["critical_path"]["duration_ms"] is None
    with pytest.raises(ValueError, match="unavailable"):
        graph.critical_path()


def test_dependency_validation_reflects_graph_mutations():
    graph = ExecutionGraph.from_trace(pipeline())
    assert graph.critical_path().node_ids
    graph.edges.append(ExecutionEdge("route", "classify", "dependency"))
    assert graph.dependency_summary()["cyclic"]
    with pytest.raises(ValueError):
        graph.critical_path()


def test_pipeline_repetition_does_not_invent_an_agent_loop():
    trace = pipeline()
    for index in range(12):
        record_operation(
            "classify",
            kind="classifier",
            duration_ms=1,
            started_at=trace.started_at,
            ended_at=trace.ended_at,
            trace=trace,
        )
    assert not any(
        item["type"] in {"runaway_loop", "tool_oscillation"}
        for item in build_diagnosis(trace)["findings"]
    )
    assert "execution" not in build_diagnosis(_quickstart_trace())


def test_workflow_and_stage_semantics_survive_replay_study_and_reports(tmp_path):
    baseline, candidate = pipeline(), pipeline(run_id="candidate", version="2")
    replay = build_replay_report(baseline, candidate)
    assert replay["baseline"]["execution"]["version"] == "1"
    assert replay["candidate"]["execution"]["version"] == "2"
    assert replay["baseline"]["stages"]["classify"]["kind"] == "classifier"
    baseline.export_json(tmp_path / "baseline.json")
    candidate.export_json(tmp_path / "candidate.json")
    manifest = {
        "schema_version": "1.0",
        "name": "workflow study",
        "baseline": "baseline",
        "conditions": {"baseline": ["baseline.json"], "candidate": ["candidate.json"]},
        "pairing_keys": ["task_id", "seed"],
    }
    path = tmp_path / "study.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    study = summarize_study(path)
    assert study["conditions"]["candidate"]["runs"][0]["execution"]["version"] == "2"
    report = baseline.report()
    export_report_markdown(report, tmp_path / "report.md")
    assert "schema:input:1" in (tmp_path / "report.md").read_text(encoding="utf-8")
    html = analysis_to_html(
        {
            "trace": baseline.to_dict(),
            "report": report,
            "diagnosis": build_diagnosis(baseline),
            "optimization": build_optimization_plan(baseline),
        }
    )
    assert "mail-router" in html and "schema:input:1" in html and "output:route" in html


@pytest.mark.parametrize(
    "status,expected",
    [
        ("failed", 0),
        ("cancelled", 0),
        ("interrupted", 0),
        ("running", None),
        ("unknown", None),
        ("completed", 1),
    ],
)
def test_study_does_not_infer_workflow_success_from_absent_error_spans(tmp_path, status, expected):
    before, after = pipeline(), pipeline(run_id="after", status=status)
    after.events.clear()
    before.export_json(tmp_path / "before.json")
    after.export_json(tmp_path / "after.json")
    data = {
        "schema_version": "1.0",
        "name": "status",
        "baseline": "before",
        "conditions": {"before": ["before.json"], "after": ["after.json"]},
        "pairing_keys": ["task_id", "seed"],
    }
    path = tmp_path / "study.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    run = summarize_study(path)["conditions"]["after"]["runs"][0]
    assert run["metrics"]["success"] == expected
    assert run["success_basis"] == "workflow_status"


def test_metadata_declarations_are_immutable_and_conflicts_are_explicit():
    info = StageInfo("stage", "1")
    with pytest.raises(FrozenInstanceError):
        info.stage_id = "other"
    with pytest.raises(ValueError, match="conflicting"):
        workflow_metadata(WorkflowInfo("wf", "1"), task_id=1, metadata={"task_id": "1"})
    with pytest.raises(ValueError, match="reserved"):
        operation_metadata("rule", metadata={STAGE_KEY: {}})
    with pytest.raises(ValueError, match="bounded"):
        with trace_workflow("wf", workflow=WorkflowInfo("wf", "1")) as trace:
            set_workflow_outcome(trace, "private output body")


def test_unknown_metadata_versions_survive_native_roundtrip():
    trace = pipeline()
    raw = copy.deepcopy(trace.to_dict())
    raw["metadata"][WORKFLOW_KEY]["schema_version"] = "future"
    raw["events"][0]["metadata"][STAGE_KEY]["schema_version"] = "future"
    loaded = AgentTrace.from_dict(raw)
    assert loaded.to_dict()["metadata"] == raw["metadata"]
    assert loaded.report()["execution"]["schema_status"] == "unsupported"
    assert stage_summary(loaded.events[0])["schema_status"] == "unsupported"


def test_stage_html_escapes_references_and_does_not_expose_other_event_metadata():
    trace = pipeline()
    stage = trace.events[0].metadata[STAGE_KEY]["stage"]
    stage["input_schema_ref"] = '<img src=x onerror="secret()">'
    stage["private_body"] = "private-stage-payload"
    trace.events[0].metadata["private_body"] = "private-event-payload"
    html = analysis_to_html(
        {
            "trace": trace.to_dict(),
            "report": trace.report(),
            "diagnosis": build_diagnosis(trace),
            "optimization": build_optimization_plan(trace),
        }
    )
    assert "<img src=x" not in html and "&lt;img" in html
    assert "private-stage-payload" not in html and "private-event-payload" not in html


def test_external_task_cancellation_records_status_without_leaking_context():
    async def scenario():
        entered = asyncio.Event()
        traces = []

        async def work():
            with trace_workflow("cancel", workflow=WorkflowInfo("wf", "1")) as trace:
                traces.append(trace)
                with trace_operation("wait", kind="external_service"):
                    entered.set()
                    await asyncio.Event().wait()

        task = asyncio.create_task(work())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert traces[0].metadata[WORKFLOW_KEY]["status"] == "cancelled"
        assert traces[0].events[0].status == "error"
        assert current_trace() is None and current_event_id() is None

    asyncio.run(scenario())
