"""Profile a synthetic decision pipeline; AgentLoop supplies no decision engine."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agentloop import StageInfo, WorkflowInfo, set_workflow_outcome, trace_operation, trace_workflow
from agentloop.findings import build_diagnosis
from agentloop.html_report import analysis_to_html
from agentloop.optimizer import build_optimization_plan
from agentloop.otel import trace_from_otel, trace_to_otel


def run(out: Path, *, branching=True):
    message = "Synthetic question about an invoice"
    with trace_workflow(
        "Message routing fixture",
        workflow=WorkflowInfo(
            "message-routing",
            "fixture-v1",
            input_schema_ref="schema:message:1",
            output_schema_ref="schema:route:1",
            input_ref="fixture:message-1",
        ),
        task_id="message-1",
        example_id="routing-example",
        metadata={"synthetic": True},
    ) as trace:
        with trace_operation(
            "Classify",
            kind="classifier",
            stage=StageInfo(
                "classify",
                "fixture-v1",
                input_schema_ref="schema:message:1",
                output_schema_ref="schema:category:1",
            ),
            depends_on=[],
        ) as classify:
            category = "billing" if "invoice" in message else "general"
            classify.set_outcome(category, output_ref="fixture:category-1")
        with trace_operation(
            "Prioritize",
            kind="rule",
            stage=StageInfo("priority", "fixture-v1"),
            depends_on=[classify.event_id],
        ) as priority:
            priority_value = "normal"
            priority.set_outcome(priority_value, output_ref="fixture:priority-1")
        dependencies = [priority.event_id]
        if branching:
            with trace_operation(
                "Look up destination",
                kind="retriever",
                stage=StageInfo("destination", "fixture-v1"),
                depends_on=[classify.event_id],
            ) as lookup:
                destination = {"billing": "billing-queue", "general": "general-queue"}[category]
                lookup.set_outcome("found", output_ref="fixture:destination-1")
            dependencies.append(lookup.event_id)
        else:
            destination = category + "-queue"
        with trace_operation(
            "Route",
            kind="transform",
            stage=StageInfo("route", "fixture-v1", output_schema_ref="schema:route:1"),
            depends_on=dependencies,
        ) as route:
            result = {"destination": destination, "priority": priority_value}
            route.set_outcome("routed", output_ref="fixture:route-1")
        set_workflow_outcome(trace, "routed", output_ref="fixture:route-1")
        trace.metadata["success"] = result == {"destination": "billing-queue", "priority": "normal"}
        trace.metadata["quality_score"] = float(trace.metadata["success"])
    out.mkdir(parents=True, exist_ok=True)
    trace.export_json(out / "trace.json")
    report = trace.report()
    payload = {
        "trace": trace.to_dict(),
        "report": report,
        "diagnosis": build_diagnosis(trace),
        "optimization": build_optimization_plan(trace),
    }
    (out / "analysis.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    (out / "analysis.html").write_text(analysis_to_html(payload), encoding="utf-8")
    otlp = trace_to_otel(trace)
    (out / "trace.otel.json").write_text(json.dumps(otlp, indent=2) + "\n", encoding="utf-8")
    restored = trace_from_otel(otlp)
    assert restored.report()["operation_counts"] == report["operation_counts"]
    assert restored.report()["execution"] == report["execution"]
    assert report["tool_call_count"] == report["model_call_count"] == 0
    assert message not in json.dumps(trace.to_dict())
    assert payload["optimization"]["graph"]["dependency_evidence"]["valid"]
    return trace


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/workflow-trace"))
    parser.add_argument("--linear", action="store_true")
    options = parser.parse_args()
    trace = run(options.out, branching=not options.linear)
    print(f"Recorded {len(trace.events)} synthetic workflow operations without raw message bodies.")
