"""Synthetic chunked extraction/classification/matching with explicit quality evidence."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from agentloop import (
    AgentTrace,
    JudgeIdentity,
    JudgeUsage,
    WorkflowInfo,
    attach_quality_report,
    set_workflow_outcome,
    trace_workflow,
)
from agentloop.entrypoint import _analysis_payload
from agentloop.findings import build_diagnosis
from agentloop.html_report import analysis_to_html
from agentloop.quality import build_quality_report
from agentloop.replay import ReplayGates, build_replay_report, replay_report_to_markdown
from agentloop.studies import study_to_markdown, summarize_study, summarize_values
from agentloop.substitution_export import export_substitution_studies
from agentloop.substitution_types import (
    DecisionImplementation,
    DecisionResult,
    DecisionStep,
    SubstitutionExample,
    SubstitutionProtocol,
)
from agentloop.substitutions import run_substitution_experiment

if __package__:
    from .reference_support import canonical, decision, digest, write_json
else:
    from reference_support import canonical, decision, digest, write_json

CONTEXT = (
    "Use the declared record schema, preserve exact units and kind, and abstain from entity matches that the fixture catalog cannot establish. "
    * 8
)
KINDS = ("widget", "gear", "service", "unlisted")
CATEGORIES = {"widget": "hardware", "gear": "hardware", "service": "service", "unlisted": "unknown"}
ENTITIES = {"alpha": "entity-a", "beta": "entity-b", "ambiguous": None, "unlisted": None}
GOLD_PATTERN = (
    ("widget", "alpha", "hardware", "entity-a"),
    ("gear", "beta", "hardware", "entity-b"),
    ("service", "ambiguous", "service", None),
    ("unlisted", "unlisted", "unknown", None),
)
VARIANTS = ("baseline", "optimized", "cheap", "failing")


def dataset(count):
    if type(count) is not int or not 4 <= count <= 10000:
        raise ValueError("records must be between 4 and 10000")
    rows = []
    for index in range(count):
        identity = f"record-{index:05d}"
        kind, alias, category, entity = GOLD_PATTERN[index % len(GOLD_PATTERN)]
        units = index * 7 % 97
        rows.append(
            {
                "id": identity,
                "text": f"kind={kind};units={units};entity={alias}",
                "expected": {
                    "fields": {"kind": kind, "units": units, "entity": alias},
                    "category": category,
                    "matches": [[identity, entity]] if entity is not None else [],
                },
            }
        )
    return rows


def extract(text, *, cheap=False):
    values = dict(part.split("=", 1) for part in text.split(";"))
    return {
        "kind": values["kind"],
        "units": int(values["units"][0] if cheap else values["units"]),
        "entity": values["entity"],
    }


def classify(fields, *, cheap=False):
    return "hardware" if cheap else CATEGORIES.get(fields["kind"], "unknown")


def match(identity, fields, *, cheap=False):
    entity = (
        ("entity-a" if fields["entity"].startswith("a") else "entity-b")
        if cheap
        else ENTITIES.get(fields["entity"])
    )
    return [[identity, entity]] if entity is not None else []


def run_chunk(rows, variant, chunk_id, fixture_hash, *, capture_prompts=False):
    if variant not in VARIANTS:
        raise ValueError("unknown data configuration")
    with trace_workflow(
        "data-reference/" + variant,
        workflow=WorkflowInfo("reference.batch-data", "1.0"),
        task_id=chunk_id,
        metadata={
            "synthetic": True,
            "fixture_hash": fixture_hash,
            "variant": variant,
            "record_count": len(rows),
            "capture_mode": "synthetic_prompts" if capture_prompts else "hashes_only",
        },
    ) as trace:
        outputs, outcomes = {}, []
        batch = None
        if variant == "optimized":
            batch = decision(
                trace,
                "extract",
                {
                    "context": CONTEXT,
                    "records": [{"id": row["id"], "text": row["text"]} for row in rows],
                },
                lambda value: {row["id"]: extract(row["text"]) for row in value["records"]},
                model_fixture=True,
                event_id="extract-batch",
                capture_input=capture_prompts,
                extra_metadata={
                    "record_ids": [row["id"] for row in rows],
                    "batch_safe": True,
                    "side_effects": "none",
                    "schema_ref": "fixture:record-fields-v1",
                },
            )
        for row in rows:
            identity = row["id"]
            output = outputs.setdefault(identity, {})
            stage = "extract"
            try:
                if batch is not None:
                    output["fields"] = batch[identity]
                    previous = "extract-batch"
                else:
                    previous = "extract-" + identity
                    output["fields"] = decision(
                        trace,
                        "extract",
                        {"context": CONTEXT, "text": row["text"]},
                        lambda value: extract(value["text"], cheap=variant == "cheap"),
                        model_fixture=variant != "cheap",
                        event_id=previous,
                        capture_input=capture_prompts,
                        extra_metadata={
                            "record_ids": [identity],
                            "batch_safe": True,
                            "side_effects": "none",
                            "schema_ref": "fixture:record-fields-v1",
                        },
                    )
                for stage, field, callback in (
                    (
                        "classify",
                        "category",
                        lambda value: classify(value["fields"], cheap=variant == "cheap"),
                    ),
                    (
                        "match",
                        "matches",
                        lambda value, identity=identity: match(
                            identity, value["fields"], cheap=variant == "cheap"
                        ),
                    ),
                ):
                    event_id = stage + "-" + identity
                    output[field] = decision(
                        trace,
                        stage,
                        {"context": CONTEXT, "fields": output["fields"]},
                        callback,
                        model_fixture=variant in {"baseline", "failing"},
                        depends_on=(previous,),
                        event_id=event_id,
                        capture_input=capture_prompts,
                        fail=variant == "failing"
                        and identity == "record-00003"
                        and stage == "match",
                        extra_metadata={
                            "record_ids": [identity],
                            "batch_safe": True,
                            "side_effects": "none",
                            "schema_ref": "fixture:" + stage + "-v1",
                        },
                    )
                    previous = event_id
                decision(
                    trace,
                    "output",
                    {"result_hash": digest(output)},
                    lambda value: value["result_hash"],
                    model_fixture=False,
                    depends_on=(previous,),
                    event_id="output-" + identity,
                    extra_metadata={"record_ids": [identity]},
                )
                outcomes.append({"record_id": identity, "status": "completed"})
            except (TimeoutError, ValueError):
                outcomes.append({"record_id": identity, "status": "failed", "failed_stage": stage})
        trace.metadata["record_outcomes"] = outcomes
        set_workflow_outcome(trace, "records_processed", output_ref="sha256:" + digest(outputs))
    return trace, outputs


def quality_for_chunk(rows, before, after, old, new, fixture_hash):
    fixtures = []
    for row in rows:
        for field, scorer in (
            ("fields", {"type": "fields", "allow_extra": False}),
            ("category", {"type": "decision", "labels": ["hardware", "service", "unknown"]}),
            ("matches", {"type": "matches", "symmetric": False}),
        ):
            fixture = {
                "schema_version": "2.0",
                "id": row["id"] + ":" + field,
                "expected": row["expected"][field],
                "expected_ref": "fixture:" + fixture_hash + ":" + row["id"] + ":" + field,
                "input_ref": "sha256:" + digest(row["text"]),
                "scorer": {**scorer, "version": "1.0"},
            }
            for side, values in (("baseline", old), ("candidate", new)):
                if field in values.get(row["id"], {}):
                    fixture[side + "_output"] = values[row["id"]][field]
            fixtures.append(fixture)
    return build_quality_report(fixtures, baseline_trace=before, candidate_trace=after, min_score=1)


def aggregate_stages(traces):
    stages = defaultdict(list)
    for trace in traces:
        models = [event for event in trace.events if event.event_type == "model_call"]
        costs = trace.report()["cost_breakdown"]["model_calls"]
        by_id = {event.event_id: value for event, value in zip(models, costs)}
        for event in trace.events:
            stages[event.name].append((event, by_id.get(event.event_id)))
    result = {}
    for stage, values in stages.items():
        errors = sum(event.status == "error" for event, _ in values)
        model_values = [(event, cost) for event, cost in values if cost is not None]
        amounts = [cost["amount_usd"] for _, cost in model_values]
        result[stage] = {
            "call_count": len(values),
            "model_call_count": len(model_values),
            "failure_count": errors,
            "failure_rate": errors / len(values),
            "latency_ms": summarize_values([event.duration_ms for event, _ in values]),
            "model_cost_usd": summarize_values(amounts),
            "known_model_cost_total_usd": sum(value for value in amounts if value is not None),
            "cost_complete": all(value is not None for value in amounts),
            "fixture_input_tokens": sum(event.input_tokens for event, _ in model_values),
            "fixture_output_tokens": sum(event.output_tokens for event, _ in model_values),
            "tokenizer": "json-whitespace-fixture-v1",
            "cost_scope": "synthetic_backend_reports_only",
        }
    return result


def substitution_demo(rows, source, out):
    step = DecisionStep.from_trace(
        source, "classify-" + rows[0]["id"], step_id="classification", version="1.0"
    )
    examples = [
        SubstitutionExample(
            row["id"],
            extract(row["text"]),
            input_ref="sha256:" + digest(row["text"]),
            quality_ref="fixture:data-category-v1:" + row["id"],
            expected=row["expected"]["category"],
        )
        for row in rows[:8]
    ]
    protocol = SubstitutionProtocol(
        "Data classification substitution",
        "1.0",
        step,
        examples,
        {"type": "decision", "version": "1.0", "labels": ["hardware", "service", "unknown"]},
        seed=7,
        timeout_s=1,
    )

    def implementation(name, cheap=False, model=False):
        def invoke(request, *, timeout_s):
            return DecisionResult(
                classify(request.inputs, cheap=cheap),
                usage=JudgeUsage(
                    20 if model else 0,
                    1 if model else 0,
                    0.00021 if model else 0,
                    "estimated" if model else "reported",
                    "calculated",
                ),
                cost_ref="synthetic:data-classification-cost-v1",
            )

        return DecisionImplementation(
            name,
            JudgeIdentity.configured(
                "example.data." + name,
                "1.0",
                {"fixture": "1.0"},
                provider="synthetic-local",
                model_or_rule=name,
            ),
            invoke,
            "model" if model else "rule",
        )

    bundle = run_substitution_experiment(
        protocol,
        implementation("model-fixture", model=True),
        [implementation("kind-rule"), implementation("cheap-guess", cheap=True)],
        enabled=True,
    )
    return export_substitution_studies(bundle, out)["comparison"]


def main(out, *, records=24, chunk_size=8, capture_prompts=False, html_samples=1):
    if type(chunk_size) is not int or not 1 <= chunk_size <= 1000:
        raise ValueError("chunk_size must be between 1 and 1000")
    if (
        type(html_samples) is not int
        or not 0 <= html_samples <= 10
        or type(capture_prompts) is not bool
    ):
        raise ValueError("invalid diagnostic capture settings")
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise FileExistsError("use an empty output directory for a new reference run")
    rows = dataset(records)
    fixture_hash = digest(rows)
    chunks = [rows[index : index + chunk_size] for index in range(0, len(rows), chunk_size)]
    traces, outputs = {}, {}
    for index, chunk in enumerate(chunks):
        for variant in VARIANTS:
            trace, result = run_chunk(
                chunk, variant, f"chunk-{index:04d}", fixture_hash, capture_prompts=capture_prompts
            )
            traces[variant, index], outputs[variant, index] = trace, result
            directory = out / "analysis" / f"{variant}-{index:04d}"
            write_json(directory / "trace.json", trace.to_dict())
            write_json(directory / "outputs.json", result)
            write_json(directory / "report.json", trace.report())
            write_json(directory / "diagnosis.json", build_diagnosis(trace))
            if index < html_samples:
                (directory / "analysis.html").write_text(
                    analysis_to_html(_analysis_payload(trace)), encoding="utf-8"
                )
    comparisons = []
    for variant in VARIANTS[1:]:
        conditions = {"baseline": [], "candidate": []}
        for index, chunk in enumerate(chunks):
            before, after = traces["baseline", index], traces[variant, index]
            quality = quality_for_chunk(
                chunk,
                before,
                after,
                outputs["baseline", index],
                outputs[variant, index],
                fixture_hash,
            )
            replay = build_replay_report(
                before,
                after,
                quality_report=quality,
                gates=ReplayGates(min_cost_improvement_pct=10, min_quality_score=1),
            )
            directory = out / variant / f"chunk-{index:04d}"
            write_json(directory / "quality.json", quality)
            write_json(directory / "replay.json", replay)
            (directory / "replay.md").write_text(
                replay_report_to_markdown(replay), encoding="utf-8"
            )
            for side, original in (("baseline", before), ("candidate", after)):
                trace = AgentTrace.from_dict(json.loads(canonical(original.to_dict())))
                attach_quality_report(trace, quality, side=side)
                relative = f"chunk-{index:04d}/{side}.json"
                write_json(out / variant / relative, trace.to_dict())
                conditions[side].append(relative)
            comparisons.append(
                {
                    "variant": variant,
                    "chunk": index,
                    "record_count": len(chunk),
                    "candidate_quality": quality["candidate_score"],
                    "unavailable_quality_cases": quality["indeterminate_case_count"],
                    "passed": replay["gates"]["passed"],
                    "deltas": replay["deltas"],
                }
            )
        manifest = {
            "schema_version": "1.0",
            "name": "Batch data reference / " + variant,
            "baseline": "baseline",
            "conditions": conditions,
            "pairing_keys": ["task_id", "fixture_hash"],
        }
        path = out / variant / "study.json"
        write_json(path, manifest)
        study = summarize_study(path)
        write_json(path.with_name("study-report.json"), study)
        path.with_name("study-report.md").write_text(study_to_markdown(study), encoding="utf-8")
    stages = {
        variant: aggregate_stages([traces[variant, index] for index in range(len(chunks))])
        for variant in VARIANTS
    }
    summary = {
        "schema_version": "1.0",
        "synthetic": True,
        "record_count": len(rows),
        "chunk_count": len(chunks),
        "chunk_size": chunk_size,
        "fixture_hash": fixture_hash,
        "per_stage": stages,
        "comparisons": comparisons,
        "record_failures": {
            variant: [
                item
                for index in range(len(chunks))
                for item in traces[variant, index].metadata["record_outcomes"]
                if item["status"] != "completed"
            ]
            for variant in VARIANTS
        },
        "trace_volume": {
            "trace_count": len(traces),
            "event_count": sum(len(trace.events) for trace in traces.values()),
            "captured_prompt_bytes": sum(
                len((event.input_text or "").encode())
                for trace in traces.values()
                for event in trace.events
            ),
            "capture_prompts": capture_prompts,
            "html_samples_per_variant": min(html_samples, len(chunks)),
            "retention_scope": "all events and outcome references; full synthetic prompts optional; HTML diagnostics limited by chunk",
        },
        "interpretation": "Executed synthetic record pipelines. Costs/token units are mock-backend fixtures; local timing and small paired chunks are exploratory. Batch-level cost is not divided into invented per-record billing.",
    }
    write_json(out / "fixtures.json", rows)
    write_json(out / "summary.json", summary)
    substitution = substitution_demo(rows, traces["baseline", 0], out / "substitution")
    assert all(
        item["candidate_quality"] == 1 for item in comparisons if item["variant"] == "optimized"
    )
    assert any(item["candidate_quality"] < 1 for item in comparisons if item["variant"] == "cheap")
    assert any(
        item["candidate_quality"] is None for item in comparisons if item["variant"] == "failing"
    )
    assert (
        next(item for item in substitution["comparisons"] if item["condition"] == "kind-rule")[
            "quality_preserved_on_examples"
        ]
        is True
    )
    print(f"Wrote {len(rows)} synthetic records in {len(chunks)} workflow chunks to {out}")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("runs/batch-data-reference"))
    parser.add_argument("--records", type=int, default=24)
    parser.add_argument("--chunk-size", type=int, default=8)
    parser.add_argument("--capture-prompts", action="store_true")
    parser.add_argument("--html-samples", type=int, default=1)
    args = parser.parse_args()
    main(
        args.out,
        records=args.records,
        chunk_size=args.chunk_size,
        capture_prompts=args.capture_prompts,
        html_samples=args.html_samples,
    )
