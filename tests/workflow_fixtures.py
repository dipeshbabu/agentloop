"""Deterministic, payload-free workflow fixtures shared across storage/UI tests."""

from datetime import datetime, timedelta, timezone

from agentloop import StageInfo, WorkflowInfo, record_operation, workflow_metadata
from agentloop.tracer import AgentTrace


def pipeline(
    *, branching=False, run_id="workflow-run", version="1", task="task-1", status="completed"
):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    trace = AgentTrace(
        name="decision pipeline",
        run_id=run_id,
        started_at=start.isoformat(),
        ended_at=(start + timedelta(milliseconds=80)).isoformat(),
        elapsed_ms=80,
        metadata=workflow_metadata(
            WorkflowInfo(
                "mail-router",
                version,
                input_schema_ref="schema:message:1",
                output_schema_ref="schema:route:1",
            ),
            task_id=task,
            example_id="example-1",
            status=status,
            metadata={"synthetic": True, "seed": 0},
        ),
    )
    steps = [("classify", "classifier", 0, 20, [])]
    if branching:
        steps.extend(
            [
                ("priority", "rule", 20, 50, ["classify"]),
                ("lookup", "retriever", 20, 60, ["classify"]),
                ("route", "external_service", 60, 80, ["priority", "lookup"]),
            ]
        )
    else:
        steps.extend(
            [
                ("priority", "rule", 20, 50, ["classify"]),
                ("route", "transform", 50, 80, ["priority"]),
            ]
        )
    for name, kind, begin, end, parents in steps:
        record_operation(
            name,
            kind=kind,
            duration_ms=end - begin,
            started_at=(start + timedelta(milliseconds=begin)).isoformat(),
            ended_at=(start + timedelta(milliseconds=end)).isoformat(),
            stage=StageInfo(
                name,
                "stage-v1",
                input_schema_ref="schema:input:1",
                output_schema_ref="schema:output:1",
                input_ref=f"input:{name}",
            ),
            outcome="recorded",
            output_ref=f"output:{name}",
            depends_on=parents,
            trace=trace,
            event_id=name,
        )
    return trace
