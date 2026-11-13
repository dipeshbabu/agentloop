"""Validate real Hermes pilot artifacts with AgentLoop's own schema and reports."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath

from agentloop.ci import build_ci_report
from agentloop.entrypoint import _analysis_payload
from agentloop.html_report import analysis_to_html
from agentloop.replay import build_replay_report
from agentloop.tracer import AgentTrace

REVISION = "b200f5bc60e31588ba2cb60e288029c4a54ba5fa"
REVISIONS = {
    REVISION: "v0.22.7",
    "3b421578588243f920886e6fe79a6faa87ea026e": "v0.23.1",
}
SCENARIOS = (
    "baseline",
    "stream",
    "tool",
    "retry",
    "interrupt",
    "tool-error",
    "privacy",
    "delegation",
)
EXPECTED_CALLS = dict(zip(SCENARIOS, (1, 1, 2, 2, 1, 2, 1, 4), strict=True))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def artifact_bytes(root: Path, relative: str, digest: str) -> bytes:
    """Reject traversal, aliases outside the run, and changed evidence bytes."""
    relative_path = PurePosixPath(relative)
    require(
        bool(relative_path.parts)
        and not relative_path.is_absolute()
        and ".." not in relative_path.parts
        and "\\" not in relative
        and ":" not in relative,
        "Invalid artifact path",
    )
    path = (root / relative).resolve()
    require(path.is_relative_to(root.resolve()), "Artifact escapes its scenario directory")
    require(path.stat().st_size <= 16 * 1024 * 1024, "Artifact exceeds the pilot size limit")
    content = path.read_bytes()
    require(hashlib.sha256(content).hexdigest() == digest, "Artifact checksum mismatch")
    return content


def qualified_trace(
    root: Path, artifact: dict, scenario: str, *, revision: str = REVISION
) -> AgentTrace:
    require(revision in REVISIONS, "Wrong Hermes source revision")
    raw = artifact_bytes(root, artifact["raw"], artifact["raw_sha256"])
    native = artifact_bytes(root, artifact["native"], artifact["native_sha256"])
    expected = json.loads(raw)
    expected["metadata"].update(
        {
            "synthetic": True,
            "source": "hermes_runtime_pilot",
            "pilot": {
                "scenario": scenario,
                "synthetic_provider": True,
                "hermes_revision": revision,
            },
        }
    )
    payload = json.loads(native)
    require(payload == expected, "Native qualification changed captured evidence")
    for canary in (
        "PRIVATE_PROMPT_CANARY",
        "PRIVATE_PROVIDER_ERROR_CANARY",
        "local-pilot-key",
        "http://127.0.0.1:",
    ):
        require(
            canary.encode() not in raw and canary.encode() not in native,
            "Private request content leaked",
        )
    trace = AgentTrace.from_dict(payload)
    require(trace.metadata.get("integration") == "hermes", "Expected Hermes evidence")
    ids = {event.event_id for event in trace.events}
    require(len(ids) == len(trace.events), "Duplicate event identity")
    require(
        all(event.parent_id is None or event.parent_id in ids for event in trace.events),
        "Missing event parent",
    )
    require(trace.ended_at is not None and trace.elapsed_ms is not None, "Missing turn boundary")
    return trace


def validate_case(root: Path, scenario: str) -> tuple[dict, list[AgentTrace]]:
    report = json.loads((root / "report.json").read_bytes())
    require(
        report["scenario"] == scenario and report["synthetic"] is True,
        "Wrong scenario qualification",
    )
    revision = report["host"]["revision"]
    require(revision in REVISIONS, "Wrong Hermes source revision")
    require(report["host"]["tag"] == REVISIONS[revision], "Wrong Hermes version tag")
    require(not report["blocked_connections"], "Host attempted an unexpected outbound connection")
    require(not report["agent_error"], "Host returned an agent error")
    requests = [item for item in report["requests"] if item["kind"] == "completion"]
    require(len(requests) == EXPECTED_CALLS[scenario], "Unexpected provider request count")
    require(
        all(
            item["stream"] is True and item["model"] == "agentloop-local-pilot" for item in requests
        ),
        "Streaming/model route changed",
    )
    require(
        [item["status"] for item in requests]
        == ([429, 200] if scenario == "retry" else [200] * len(requests)),
        "Unexpected provider status",
    )
    if scenario == "interrupt":
        require(
            report["interrupted"]
            and report["interrupt_calls"] == 1
            and report["stream_callbacks"] > 0,
            "Real streamed interruption was not exercised",
        )
    else:
        require(
            report["completion_matches_fixture"] and not report["interrupted"],
            "Host result differs from fixture",
        )
    traces = [
        qualified_trace(root, item, scenario, revision=revision) for item in report["artifacts"]
    ]
    require(
        len(traces) == (0 if scenario == "baseline" else 2 if scenario == "delegation" else 1),
        "Unexpected exported turn count",
    )
    roots = [
        trace
        for trace in traces
        if trace.metadata["hermes"]["session_id"] == report["root_session_id"]
    ]
    require(len(roots) == (0 if scenario == "baseline" else 1), "Root session linkage changed")
    model_events = [
        event for trace in traces for event in trace.events if event.event_type == "model_call"
    ]
    require(
        len(model_events) == (0 if scenario in ("baseline", "interrupt") else len(requests)),
        "Model attempt capture is incomplete or duplicated",
    )
    for event in model_events:
        expected = (0, 0, "unavailable") if event.status == "error" else (17, 5, "provider")
        require(
            (event.input_tokens, event.output_tokens, event.token_provenance) == expected,
            "Normalized provider usage changed",
        )
    for trace in traces:
        capture = trace.metadata["hermes"]
        interrupted = scenario == "interrupt"
        require(capture["capture_complete"] is (not interrupted), "Capture completeness changed")
        require(
            capture["turn_outcome"] == ("interrupted" if interrupted else "completed"),
            "Host outcome changed",
        )
        require(
            capture["unfinished_requests"] == (1 if interrupted else 0)
            and capture["events_dropped"] == 0,
            "Unfinished/dropped request evidence changed",
        )
        root_event = [event for event in trace.events if event.parent_id is None]
        require(
            len(root_event) == 1 and root_event[0].metadata.get("operation_kind") == "agent",
            "Missing native turn root",
        )
        require(root_event[0].status == ("error" if interrupted else "ok"), "Turn status changed")
        if scenario == "privacy":
            require(
                capture["capture_content"] is False and "output" not in trace.metadata,
                "Default output privacy changed",
            )
            require(
                all(event.output_text is None for event in trace.events),
                "Default privacy retained output",
            )
            require(
                "PRIVATE_OUTPUT_CANARY" not in json.dumps(trace.to_dict()), "Private output leaked"
            )
        analysis = _analysis_payload(trace)
        html_path = root / (trace.run_id + ".html")
        html_path.write_text(analysis_to_html(analysis), encoding="utf-8")
    if scenario == "retry":
        require(
            len([event for event in model_events if event.status == "error"]) == 1,
            "Failed provider attempt was lost",
        )
        require(
            sum(event.event_type == "retry" for event in traces[0].events) == 1,
            "Retry evidence was lost",
        )
    if scenario in ("tool", "tool-error", "delegation"):
        reads = [event for trace in traces for event in trace.events if event.name == "read_file"]
        require(
            len(reads) == 1 and reads[0].status == ("error" if scenario == "tool-error" else "ok"),
            "Real file tool outcome was lost",
        )
    if scenario == "delegation":
        parent = roots[0]
        child = next(trace for trace in traces if trace is not parent)
        links = parent.metadata["hermes"].get("children", {}).values()
        require(
            any(
                link.get("child_session_id") == child.metadata["hermes"]["session_id"]
                for link in links
            ),
            "Delegated session link was lost",
        )
        require(
            parent.metadata.get("output") == "AGENTLOOP_RUNTIME_PILOT_OK"
            and child.metadata.get("output") == "CHILD_PILOT_OK",
            "Delegated output changed",
        )
        for trace in traces:
            metrics = trace.report()
            require(
                (metrics["model_call_count"], metrics["input_tokens"], metrics["output_tokens"])
                == (2, 34, 10),
                "Child usage was duplicated or lost",
            )
    if scenario == "interrupt":
        replay = build_replay_report(traces[0], traces[0])
        capture_gates = [
            gate
            for gate in replay["gates"]["results"]
            if gate["name"].endswith("_capture_complete")
        ]
        require(
            len(capture_gates) == 2 and all(gate["passed"] is False for gate in capture_gates),
            "Incomplete capture passed replay integrity gates",
        )
        require(
            build_ci_report(traces[0], traces[0])["passed"] is False, "Incomplete capture passed CI"
        )
    # Host completion is separate from task correctness, even with synthetic tasks.
    quality = (
        "not_evaluated"
        if scenario == "privacy"
        else "failed"
        if scenario in ("tool-error", "interrupt")
        else "passed"
    )
    return {
        "scenario": scenario,
        "passed": True,
        "trace_count": len(traces),
        "provider_attempts": len(requests),
        "captured_model_attempts": len(model_events),
        "fixture_task_quality": quality,
        "outer_turn_wall_ms": report["outer_turn_wall_ms"],
        "timing_scope": report["timing_scope"],
    }, traces


def validate_run(root: Path) -> dict:
    cases = [validate_case(root / scenario, scenario)[0] for scenario in SCENARIOS]
    reports = [json.loads((root / scenario / "report.json").read_bytes()) for scenario in SCENARIOS]
    require(
        len({report["host"]["revision"] for report in reports}) == 1,
        "Host source changed between scenarios",
    )
    require(
        len({report["plugin_recorder_sha256"] for report in reports}) == 1,
        "Observer bytes changed between scenarios",
    )
    require(
        len({report["host"]["lock_sha256"] for report in reports}) == 1,
        "Host dependency lock changed between scenarios",
    )
    inputs = [
        [item["user_input_sha256"] for item in report["requests"] if item["kind"] == "completion"]
        for report in reports[:2]
    ]
    require(inputs[0] == inputs[1], "Observer changed the baseline user input")
    return {
        "synthetic": True,
        "passed": True,
        "hermes_revision": reports[0]["host"]["revision"],
        "scope": "Real pinned Hermes dispatch with a local deterministic provider; no live model quality, cost or performance evidence",
        "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    result = validate_run(args.root.resolve())
    with (args.root / "validation.json").open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
