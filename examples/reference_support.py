"""Local reference-workload helpers, deliberately outside the AgentLoop core API."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from time import perf_counter

from agentloop import AgentTrace, StageInfo, attach_quality_report, record_operation
from agentloop.entrypoint import _analysis_payload
from agentloop.events import utc_now_iso
from agentloop.findings import build_diagnosis, diagnosis_to_markdown
from agentloop.html_report import analysis_to_html
from agentloop.quality import build_quality_report
from agentloop.replay import ReplayGates, build_replay_report, replay_report_to_markdown
from agentloop.studies import study_to_markdown, summarize_study


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode()).hexdigest()


def fixture_tokens(value):
    """Exact units of this mock backend's JSON-whitespace tokenizer, not an LLM proxy."""
    return len(canonical(value).split())


def decision(trace, stage_id, inputs, callback, *, model_fixture, depends_on=(), fail=False):
    started = utc_now_iso()
    began = perf_counter()
    output, status = None, "ok"
    try:
        if fail:
            raise TimeoutError("synthetic fixture timeout")
        output = callback(inputs)
        return output
    except Exception:
        status = "error"
        raise
    finally:
        elapsed = (perf_counter() - began) * 1000
        ended = utc_now_iso()
        metadata = {
            "synthetic": True,
            "input_hash": digest(inputs),
            "implementation": "fixture-model-v1" if model_fixture else "local-rule-v1",
        }
        if status == "error":
            metadata["error_type"] = "fixture_timeout"
        input_tokens = fixture_tokens(inputs) if model_fixture else None
        output_tokens = (
            fixture_tokens(output)
            if model_fixture and status == "ok"
            else (0 if model_fixture else None)
        )
        if model_fixture:
            metadata.update(
                provider="agentloop-fixture",
                tokenizer="json-whitespace-fixture-v1",
                cost_provenance={
                    "basis": "reported_by_synthetic_backend",
                    "rate_card": "synthetic:reference-rate-v1",
                    "usd_per_fixture_token": 0.00001,
                },
            )
            if status == "ok":
                metadata["provider_reported_cost_usd"] = (input_tokens + output_tokens) / 100000
        record_operation(
            stage_id,
            kind="model" if model_fixture else "rule",
            duration_ms=elapsed,
            started_at=started,
            ended_at=ended,
            trace=trace,
            event_id=stage_id,
            stage=StageInfo(
                stage_id,
                metadata["implementation"],
                kind="classifier" if model_fixture else "rule",
                input_ref="sha256:" + digest(inputs),
            ),
            depends_on=list(depends_on),
            outcome="completed" if status == "ok" else "failed",
            output_ref="sha256:" + digest(output) if status == "ok" else None,
            model="synthetic-reference-model" if model_fixture else None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            token_provenance="tokenizer" if model_fixture else None,
            status=status,
            metadata=metadata,
        )


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )


def write_reference_bundle(out, *, name, fixtures, variants, run_case, inspect_trace=None):
    """Execute actual local configurations, score frozen gold, and export native artifacts."""
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise FileExistsError("use an empty output directory for a new reference run")
    frozen = json.loads(canonical(fixtures))
    fixture_hash = digest(frozen)
    traces, summaries = {}, []
    for index, fixture in enumerate(frozen):
        for variant in variants:
            trace = run_case(
                json.loads(canonical(fixture["input"])), variant, fixture["id"], fixture_hash
            )
            if inspect_trace is not None:
                inspect_trace(trace, fixture["input"], variant)
            traces[variant, fixture["id"]] = trace
    manifests = []
    for variant in variants:
        if variant == "baseline":
            continue
        paired_conditions = {"baseline": [], "candidate": []}
        for index, fixture in enumerate(frozen):
            before = traces["baseline", fixture["id"]]
            after = traces[variant, fixture["id"]]
            quality = build_quality_report(
                [
                    {
                        "schema_version": "2.0",
                        "id": fixture["id"],
                        "input_ref": "sha256:" + digest(fixture["input"]),
                        "expected_ref": "fixture:" + fixture_hash + ":" + fixture["id"],
                        "expected": fixture["expected"],
                        "scorer": {"type": "fields", "version": "1.0", "allow_extra": False},
                    }
                ],
                baseline_trace=before,
                candidate_trace=after,
                min_score=1,
            )
            # Keep timing gates honest. Their pass/fail can vary for short local
            # callbacks; assert deterministic quality/cost properties, not speed.
            replay = build_replay_report(
                before,
                after,
                quality_report=quality,
                gates=ReplayGates(min_cost_improvement_pct=10, min_quality_score=1),
            )
            directory = out / variant / f"case-{index:04d}"
            write_json(directory / "quality.json", quality)
            write_json(directory / "replay.json", replay)
            (directory / "replay.md").write_text(
                replay_report_to_markdown(replay), encoding="utf-8"
            )
            for side, trace in (("baseline", before), ("candidate", after)):
                owned = AgentTrace.from_dict(json.loads(canonical(trace.to_dict())))
                attach_quality_report(owned, quality, side=side)
                filename = f"case-{index:04d}/{side}.json"
                write_json(out / variant / filename, owned.to_dict())
                paired_conditions[side].append(filename)
            summaries.append(
                {
                    "variant": variant,
                    "task_id": fixture["id"],
                    "passed": replay["gates"]["passed"],
                    "candidate_quality": quality["candidate_score"],
                    "baseline_fixture_cost_usd": before.report()["estimated_cost_usd"],
                    "candidate_fixture_cost_usd": after.report()["estimated_cost_usd"],
                    "latency_delta_ms": replay["deltas"]["runtime_ms_delta"],
                }
            )
        manifest = {
            "schema_version": "1.0",
            "name": name + " / " + variant,
            "baseline": "baseline",
            "conditions": paired_conditions,
            "pairing_keys": ["task_id", "fixture_hash"],
        }
        path = out / variant / "study.json"
        write_json(path, manifest)
        study = summarize_study(path)
        write_json(path.with_name("study-report.json"), study)
        path.with_name("study-report.md").write_text(study_to_markdown(study), encoding="utf-8")
        manifests.append(str(path))
    for index, fixture in enumerate(frozen):
        for variant in variants:
            trace = traces[variant, fixture["id"]]
            directory = out / "analysis" / f"{variant}-{index:04d}"
            diagnosis = build_diagnosis(trace)
            write_json(directory / "trace.json", trace.to_dict())
            write_json(directory / "report.json", trace.report())
            write_json(directory / "diagnosis.json", diagnosis)
            (directory / "diagnosis.md").write_text(
                diagnosis_to_markdown(diagnosis), encoding="utf-8"
            )
            (directory / "analysis.html").write_text(
                analysis_to_html(_analysis_payload(trace)), encoding="utf-8"
            )
    summary = {
        "schema_version": "1.0",
        "name": name,
        "synthetic": True,
        "fixture_hash": fixture_hash,
        "comparisons": summaries,
        "studies": manifests,
        "interpretation": "Executed deterministic fixture pipelines with independent frozen labels. Model token/cost values come from a synthetic backend and do not establish real-provider savings. Local timings vary and are not generalized performance evidence.",
    }
    write_json(out / "fixtures.json", frozen)
    write_json(out / "summary.json", summary)
    return summary
