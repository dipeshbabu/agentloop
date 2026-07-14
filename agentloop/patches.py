from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentloop.findings import build_diagnosis

SUPPORTED_PATCH_TYPES = {
    "parallelize_tools",
    "cache_context",
    "add_schema_validation",
    "batch_model_calls",
    "route_to_smaller_model",
    "split_large_step",
    "runaway_loop",
    "tool_oscillation",
}
SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx"}
SKIP_DIRS = {".git", ".pytest_cache", ".venv", "__pycache__", "agentloop.egg-info", "runs"}


@dataclass
class FileCandidate:
    path: str
    symbols: list[str]
    confidence: str
    reason: str
    locations: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "symbols": self.symbols,
            "confidence": self.confidence,
            "reason": self.reason,
            "locations": self.locations,
        }


@dataclass
class PatchPlan:
    patch_id: str
    finding_id: str
    type: str
    title: str
    risk: str
    framework: str
    files: list[FileCandidate]
    evidence_spans: list[str]
    before_pattern: str
    proposed_rewrite: str
    suggested_diff: str
    validation_command: str
    acceptance_criteria: str
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "patch_id": self.patch_id,
            "finding_id": self.finding_id,
            "type": self.type,
            "title": self.title,
            "risk": self.risk,
            "framework": self.framework,
            "files": [candidate.to_dict() for candidate in self.files],
            "evidence_spans": self.evidence_spans,
            "before_pattern": self.before_pattern,
            "proposed_rewrite": self.proposed_rewrite,
            "suggested_diff": self.suggested_diff,
            "validation_command": self.validation_command,
            "acceptance_criteria": self.acceptance_criteria,
            "notes": self.notes,
        }


def build_patch_plan(
    trace: Any,
    repo_path: str | Path = ".",
    *,
    allowed_root: str | Path | None = None,
) -> dict[str, Any]:
    repo = _resolve_repository_path(repo_path, allowed_root=allowed_root)
    diagnosis = build_diagnosis(trace)
    source_index = _index_source_files(repo)
    frameworks = _detect_frameworks(source_index)
    plans: list[PatchPlan] = []
    unsupported: list[dict[str, Any]] = []

    for finding in diagnosis.get("findings", []):
        finding_type = str(finding.get("type", ""))
        if finding_type not in SUPPORTED_PATCH_TYPES or not finding.get("rewrite", {}).get(
            "patchable"
        ):
            unsupported.append(
                _unsupported_finding(
                    finding, "finding type is not supported by dry-run patch planning yet"
                )
            )
            continue
        plans.append(_plan_for_finding(finding, source_index, frameworks, len(plans) + 1))

    return {
        "run_id": diagnosis["run_id"],
        "name": diagnosis["name"],
        "dry_run": True,
        "repo_path": str(repo),
        "summary": {
            "patch_count": len(plans),
            "unsupported_finding_count": len(unsupported),
            "frameworks_detected": frameworks,
            "supported_patch_types": sorted(SUPPORTED_PATCH_TYPES),
        },
        "patch_plans": [plan.to_dict() for plan in plans],
        "unsupported_findings": unsupported,
    }


def patch_plan_to_markdown(plan: dict[str, Any]) -> str:
    summary = plan["summary"]
    lines = [
        f"# AgentLoop Patch Plan: {plan['name']}",
        "",
        f"- Run ID: `{plan['run_id']}`",
        f"- Dry run: {plan['dry_run']}",
        f"- Repo: `{plan['repo_path']}`",
        f"- Patch plans: {summary['patch_count']}",
        f"- Unsupported findings: {summary['unsupported_finding_count']}",
        f"- Frameworks detected: {', '.join(summary['frameworks_detected']) or 'none'}",
        "",
        "## Patch Plans",
        "",
    ]
    patch_plans = plan.get("patch_plans", [])
    if not patch_plans:
        lines.append("No supported patch plans were generated.")
    for item in patch_plans:
        lines.extend(
            [
                f"### {item['patch_id']}: {item['title']}",
                "",
                f"- Finding: `{item['finding_id']}`",
                f"- Type: `{item['type']}`",
                f"- Risk: {item['risk']}",
                f"- Framework: {item['framework']}",
                f"- Evidence spans: {', '.join(item['evidence_spans']) or 'none'}",
                f"- Before pattern: {item['before_pattern']}",
                f"- Proposed rewrite: {item['proposed_rewrite']}",
                f"- Validation command: `{item['validation_command']}`",
                f"- Acceptance criteria: {item['acceptance_criteria']}",
                "",
                "Suggested diff shape:",
                "",
                "```text",
                item["suggested_diff"],
                "```",
                "",
                "Likely files:",
                "",
            ]
        )
        if not item["files"]:
            lines.extend(["- No likely source file found from trace span names.", ""])
        for candidate in item["files"]:
            symbols = ", ".join(candidate["symbols"]) or "unknown"
            location = _format_locations(candidate.get("locations", []))
            lines.append(
                f"- `{candidate['path']}`{location} ({candidate['confidence']}): "
                f"{symbols}. {candidate['reason']}"
            )
        if item["notes"]:
            lines.extend(["", "Notes:", ""])
            for note in item["notes"]:
                lines.append(f"- {note}")
        lines.append("")

    unsupported = plan.get("unsupported_findings", [])
    if unsupported:
        lines.extend(["## Unsupported Findings", ""])
        for item in unsupported:
            lines.append(f"- `{item['finding_id']}` `{item['type']}`: {item['reason']}")
    return "\n".join(lines).rstrip() + "\n"


def _plan_for_finding(
    finding: dict[str, Any],
    source_index: list[dict[str, Any]],
    frameworks: list[str],
    index: int,
) -> PatchPlan:
    finding_type = str(finding["type"])
    evidence_names = _evidence_names(finding)
    files = _candidate_files(source_index, evidence_names)
    framework = _select_framework(frameworks, files)
    templates = _rewrite_templates(finding_type, framework)
    return PatchPlan(
        patch_id=f"patch_{index:03d}",
        finding_id=str(finding["finding_id"]),
        type=finding_type,
        title=str(finding["title"]),
        risk=templates["risk"],
        framework=framework,
        files=files,
        evidence_spans=list(finding.get("affected_spans", [])),
        before_pattern=templates["before_pattern"],
        proposed_rewrite=templates["proposed_rewrite"],
        suggested_diff=templates["suggested_diff"],
        validation_command=str(finding.get("validation", {}).get("command") or "agentloop replay"),
        acceptance_criteria=str(
            finding.get("validation", {}).get("acceptance_criteria")
            or "before/after replay passes configured quality and budget gates"
        ),
        notes=_notes(finding_type, files),
    )


def _resolve_repository_path(
    repo_path: str | Path,
    *,
    allowed_root: str | Path | None,
) -> Path:
    root_input = Path.cwd() if allowed_root is None else allowed_root
    root = os.path.normcase(os.path.realpath(os.fspath(root_input)))
    root_prefix = root.rstrip(os.sep) + os.sep
    candidate = os.path.normcase(os.path.realpath(os.path.join(root, os.fspath(repo_path))))
    candidate_with_separator = candidate.rstrip(os.sep) + os.sep
    if not candidate_with_separator.startswith(root_prefix):
        raise ValueError("repository path must remain within the allowed root")
    if not os.path.isdir(candidate_with_separator):
        raise ValueError("repository path must identify an existing directory")
    return Path(candidate_with_separator)


def _index_source_files(repo: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    root = os.path.normcase(os.path.realpath(os.fspath(repo)))
    root_prefix = root.rstrip(os.sep) + os.sep
    for directory, directory_names, filenames in os.walk(root, followlinks=False):
        directory_names[:] = [
            name
            for name in directory_names
            if name not in SKIP_DIRS and not os.path.islink(os.path.join(directory, name))
        ]
        for filename in filenames:
            suffix = Path(filename).suffix
            if suffix not in SOURCE_SUFFIXES:
                continue
            joined_path = os.path.join(directory, filename)
            if os.path.islink(joined_path):
                continue
            source_path = os.path.normcase(os.path.realpath(joined_path))
            if not source_path.startswith(root_prefix):
                continue
            if not os.path.isfile(source_path):
                continue
            try:
                with open(source_path, encoding="utf-8") as source_file:
                    text = source_file.read()
            except (OSError, UnicodeDecodeError):
                continue
            relative_path = Path(os.path.relpath(source_path, root)).as_posix()
            out.append(
                {
                    "path": relative_path,
                    "suffix": suffix,
                    "text": text,
                    "symbols": _python_symbols(text) if suffix == ".py" else [],
                    "frameworks": _framework_markers(text),
                }
            )
    return out


def _python_symbols(text: str) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    symbols: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            symbols.append(
                {"name": node.name, "line": node.lineno, "kind": node.__class__.__name__}
            )
    return symbols


def _framework_markers(text: str) -> list[str]:
    markers: list[str] = []
    lowered = text.lower()
    if "stategraph" in text or "langgraph" in lowered:
        markers.append("langgraph")
    if "openai.agents" in lowered or "from agents import" in lowered or "runner.run" in lowered:
        markers.append("openai_agents")
    if "agentloop.trace" in lowered or "@agentloop.trace" in lowered:
        markers.append("plain_python")
    return markers


def _detect_frameworks(source_index: list[dict[str, Any]]) -> list[str]:
    frameworks = set()
    for item in source_index:
        frameworks.update(item["frameworks"])
    if source_index:
        frameworks.add("plain_python")
    return sorted(frameworks)


def _candidate_files(
    source_index: list[dict[str, Any]], evidence_names: list[str]
) -> list[FileCandidate]:
    candidates: list[FileCandidate] = []
    for item in source_index:
        symbol_hits = [
            symbol
            for symbol in item["symbols"]
            if symbol["name"] in evidence_names
            or any(symbol["name"].endswith(f"_{name}") for name in evidence_names)
        ]
        text_hits = [name for name in evidence_names if name and name in item["text"]]
        if not symbol_hits and not text_hits:
            continue
        confidence = "high" if symbol_hits else "medium"
        symbols = sorted({symbol["name"] for symbol in symbol_hits} or set(text_hits))
        reason = (
            "matched Python function/class names from trace evidence"
            if symbol_hits
            else "matched trace span names in source text"
        )
        locations = [
            {"symbol": symbol["name"], "line": symbol["line"], "kind": symbol["kind"]}
            for symbol in symbol_hits
        ]
        candidates.append(
            FileCandidate(
                path=item["path"],
                symbols=symbols,
                confidence=confidence,
                reason=reason,
                locations=locations,
            )
        )
    return candidates[:8]


def _select_framework(frameworks: list[str], files: list[FileCandidate]) -> str:
    if "langgraph" in frameworks and any(
        "langgraph" in file.path.lower() or "graph" in file.path.lower() for file in files
    ):
        return "langgraph"
    if "openai_agents" in frameworks:
        return "openai_agents"
    if "langgraph" in frameworks:
        return "langgraph"
    return "plain_python"


def _evidence_names(finding: dict[str, Any]) -> list[str]:
    names = []
    for row in finding.get("evidence", []):
        name = str(row.get("name") or "").split(".")[-1]
        if name:
            names.append(name)
    return sorted(set(names))


def _rewrite_templates(finding_type: str, framework: str) -> dict[str, str]:
    if finding_type == "parallelize_tools":
        return _parallelize_template(framework)
    if finding_type == "cache_context":
        return _cache_template(framework)
    if finding_type == "add_schema_validation":
        return _schema_template(framework)
    if finding_type == "batch_model_calls":
        return _batch_template(framework)
    if finding_type == "route_to_smaller_model":
        return _routing_template(framework)
    if finding_type == "split_large_step":
        return _split_template(framework)
    if finding_type == "runaway_loop":
        return _runaway_template(framework)
    if finding_type == "tool_oscillation":
        return _oscillation_template(framework)
    return {
        "risk": "medium",
        "before_pattern": "Trace shows a patchable optimization finding.",
        "proposed_rewrite": "Apply the rewrite hinted by the finding, then validate with replay.",
        "suggested_diff": "Apply the finding rewrite, then compare baseline and candidate traces with agentloop replay.",
    }


def _parallelize_template(framework: str) -> dict[str, str]:
    if framework == "langgraph":
        rewrite = "Fan out independent work with parallel LangGraph branches or Send/map nodes, then join before synthesis."
        suggested = "Replace a serial node loop with parallel Send/map fan-out and join results before the synthesis node."
    elif framework == "openai_agents":
        rewrite = "Run independent tool calls with asyncio.gather inside the agent tool orchestration step before returning combined results."
        suggested = "Wrap independent tool invocations in asyncio.gather and return the combined tool results to the agent."
    else:
        rewrite = "Replace serial independent tool calls with asyncio.gather for async tools or ThreadPoolExecutor for sync tools."
        suggested = "Before: for item in items: results.append(tool(item))\nAfter: results = await asyncio.gather(*(tool(item) for item in items))"
    return {
        "risk": "medium",
        "before_pattern": "Three or more same-name tool calls appear serial and independent in the trace.",
        "proposed_rewrite": rewrite,
        "suggested_diff": suggested,
    }


def _cache_template(framework: str) -> dict[str, str]:
    if framework == "openai_agents":
        rewrite = "Move stable instructions into agent instructions or a cached prompt prefix; pass only run-specific inputs per step."
        suggested = "Move stable prompt text into Agent instructions or a cached prefix; keep only task-specific data in each model call."
    else:
        rewrite = "Hoist repeated stable prompt/context into a constant, cached prefix, summary artifact, or provider prompt-cache boundary."
        suggested = "Before: prompt = stable_context + dynamic_input on every call\nAfter: cached_prefix = stable_context; prompt = cached_prefix + dynamic_input"
    return {
        "risk": "low",
        "before_pattern": "Multiple model calls resend the same stable context or instruction prefix.",
        "proposed_rewrite": rewrite,
        "suggested_diff": suggested,
    }


def _schema_template(framework: str) -> dict[str, str]:
    if framework == "openai_agents":
        rewrite = "Use structured outputs or output_type validation, then repair invalid output with a cheap correction step before a full retry."
        suggested = "Add output_type/schema validation and route validation failures to a small repair call before retrying the full step."
    else:
        rewrite = "Add JSON schema or Pydantic validation plus a small repair prompt before rerunning the full model step."
        suggested = "Validate model output against a schema; on failure, run one bounded repair prompt before a full retry."
    return {
        "risk": "medium",
        "before_pattern": "Retry spans indicate invalid or unusable model/tool output caused expensive reruns.",
        "proposed_rewrite": rewrite,
        "suggested_diff": suggested,
    }


def _batch_template(framework: str) -> dict[str, str]:
    if framework == "langgraph":
        rewrite = "Replace repeated same-role model nodes with a map/batch node and one join step when independent outputs can share a prompt."
        suggested = "Group repeated items into one batch prompt or one LangGraph map node, then unpack item-level results before synthesis."
    else:
        rewrite = "Batch repeated summarization, extraction, or classification calls into one prompt or bounded chunks."
        suggested = "Before: for item in items: summarize(item)\nAfter: summarize_batch(items) with structured per-item output"
    return {
        "risk": "medium",
        "before_pattern": "Three or more same-name model calls repeat the same operation shape.",
        "proposed_rewrite": rewrite,
        "suggested_diff": suggested,
    }


def _routing_template(framework: str) -> dict[str, str]:
    if framework == "openai_agents":
        rewrite = "Route small planning, extraction, verification, or summarization steps to a cheaper model while preserving the final model for synthesis."
        suggested = "Set the inexpensive step model on the Agent/tool call and keep replay gates plus a quality scorer for acceptance."
    else:
        rewrite = "Add a model router for low-token, low-risk steps and validate with quality gates before making it the default."
        suggested = "Before: model='large-model' for every step\nAfter: model=router.pick(step_kind, token_count, risk_level)"
    return {
        "risk": "medium",
        "before_pattern": "A small model step uses an expensive model despite low token volume.",
        "proposed_rewrite": rewrite,
        "suggested_diff": suggested,
    }


def _split_template(framework: str) -> dict[str, str]:
    rewrite = "Split oversized reasoning into retrieve/filter/compress/finalize stages so the final call receives smaller, higher-signal context."
    suggested = (
        "Before: final_answer(full_context)\n"
        "After: candidates = retrieve(full_context); summary = compress(candidates); final_answer(summary)"
    )
    if framework == "langgraph":
        suggested = "Insert filter and compression nodes before the final synthesis node; replay must preserve answer quality."
    return {
        "risk": "medium",
        "before_pattern": "A model span carries a very large context window or fragile all-in-one reasoning step.",
        "proposed_rewrite": rewrite,
        "suggested_diff": suggested,
    }


def _runaway_template(framework: str) -> dict[str, str]:
    if framework == "langgraph":
        rewrite = "Add explicit max-iteration, max-cost, and unchanged-state stop conditions to the cyclic graph edge."
        suggested = "Gate the loop edge on iteration_count, budget_remaining, and state_changed before routing back to the same node."
    else:
        rewrite = "Add bounded-loop guards around the agent step: max iterations, max cost, max retries, and unchanged-state detection."
        suggested = "Before: while not done: step()\nAfter: while not done and guard.allow(state): step(); guard.record(state)"
    return {
        "risk": "medium",
        "before_pattern": "The same step repeats many times in one run without an obvious convergence boundary.",
        "proposed_rewrite": rewrite,
        "suggested_diff": suggested,
    }


def _oscillation_template(framework: str) -> dict[str, str]:
    rewrite = "Add a decision memo or state-change check so the agent does not alternate between equivalent tool calls."
    suggested = (
        "Before: tool_a(); tool_b(); tool_a(); tool_b()\n"
        "After: if transition_key not in seen and state_changed: run_next_tool(); seen.add(transition_key)"
    )
    if framework == "langgraph":
        suggested = "Store a transition key in graph state and block repeated A/B/A/B routes unless state materially changed."
    return {
        "risk": "medium",
        "before_pattern": "Tool calls alternate between the same two tools, suggesting the workflow is revisiting equivalent state.",
        "proposed_rewrite": rewrite,
        "suggested_diff": suggested,
    }


def _format_locations(locations: list[dict[str, Any]]) -> str:
    if not locations:
        return ""
    formatted = ", ".join(
        f"{location.get('symbol', 'symbol')}:{location.get('line', '?')}"
        for location in locations[:3]
    )
    return f" lines {formatted}"


def _notes(finding_type: str, files: list[FileCandidate]) -> list[str]:
    notes = ["Dry-run only: no files were modified."]
    if not files:
        notes.append(
            "Add explicit span names that match source function names to improve file targeting."
        )
    if finding_type == "parallelize_tools":
        notes.append(
            "Only parallelize calls that do not mutate shared state and do not depend on each other's outputs."
        )
    if finding_type == "cache_context":
        notes.append(
            "Confirm cached context has the same invalidation boundary as the original prompt."
        )
    if finding_type == "add_schema_validation":
        notes.append("Keep the repair path cheaper than a full retry and cap repair attempts.")
    if finding_type == "batch_model_calls":
        notes.append(
            "Batch only items whose outputs can be validated independently after unpacking."
        )
    if finding_type == "route_to_smaller_model":
        notes.append(
            "Require a quality scorer or golden fixture before routing production traffic to the cheaper model."
        )
    if finding_type == "split_large_step":
        notes.append("Preserve citations or source references across the compression boundary.")
    if finding_type == "runaway_loop":
        notes.append(
            "Record the stop reason in trace metadata so future runs can distinguish healthy exits from guardrail exits."
        )
    if finding_type == "tool_oscillation":
        notes.append(
            "Make the state-change predicate domain-specific; generic duplicate suppression can hide real progress."
        )
    return notes


def _unsupported_finding(finding: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "finding_id": finding.get("finding_id"),
        "type": finding.get("type"),
        "title": finding.get("title"),
        "reason": reason,
    }
