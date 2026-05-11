from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentloop.findings import build_diagnosis

SUPPORTED_PATCH_TYPES = {"parallelize_tools", "cache_context", "add_schema_validation"}
SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx"}
SKIP_DIRS = {".git", ".pytest_cache", ".venv", "__pycache__", "agentloop.egg-info", "runs"}


@dataclass
class FileCandidate:
    path: str
    symbols: list[str]
    confidence: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "symbols": self.symbols,
            "confidence": self.confidence,
            "reason": self.reason,
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
    before_pattern: str
    proposed_rewrite: str
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
            "before_pattern": self.before_pattern,
            "proposed_rewrite": self.proposed_rewrite,
            "validation_command": self.validation_command,
            "acceptance_criteria": self.acceptance_criteria,
            "notes": self.notes,
        }


def build_patch_plan(trace: Any, repo_path: str | Path = ".") -> dict[str, Any]:
    repo = Path(repo_path).resolve()
    diagnosis = build_diagnosis(trace)
    source_index = _index_source_files(repo)
    frameworks = _detect_frameworks(source_index)
    plans: list[PatchPlan] = []
    unsupported: list[dict[str, Any]] = []

    for finding in diagnosis.get("findings", []):
        finding_type = str(finding.get("type", ""))
        if finding_type not in SUPPORTED_PATCH_TYPES or not finding.get("rewrite", {}).get("patchable"):
            unsupported.append(_unsupported_finding(finding, "finding type is not supported by dry-run patch planning yet"))
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
                f"- Before pattern: {item['before_pattern']}",
                f"- Proposed rewrite: {item['proposed_rewrite']}",
                f"- Validation command: `{item['validation_command']}`",
                f"- Acceptance criteria: {item['acceptance_criteria']}",
                "",
                "Likely files:",
                "",
            ]
        )
        if not item["files"]:
            lines.extend(["- No likely source file found from trace span names.", ""])
        for candidate in item["files"]:
            symbols = ", ".join(candidate["symbols"]) or "unknown"
            lines.append(f"- `{candidate['path']}` ({candidate['confidence']}): {symbols}. {candidate['reason']}")
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
        before_pattern=templates["before_pattern"],
        proposed_rewrite=templates["proposed_rewrite"],
        validation_command=str(finding.get("validation", {}).get("command") or "agentloop replay"),
        acceptance_criteria=str(
            finding.get("validation", {}).get("acceptance_criteria")
            or "before/after replay passes configured quality and budget gates"
        ),
        notes=_notes(finding_type, files),
    )


def _index_source_files(repo: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not repo.exists():
        return out
    for path in repo.rglob("*"):
        if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
            continue
        relative = path.relative_to(repo)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        relative_path = relative.as_posix()
        out.append(
            {
                "path": relative_path,
                "suffix": path.suffix,
                "text": text,
                "symbols": _python_symbols(text) if path.suffix == ".py" else [],
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
            symbols.append({"name": node.name, "line": node.lineno, "kind": node.__class__.__name__})
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


def _candidate_files(source_index: list[dict[str, Any]], evidence_names: list[str]) -> list[FileCandidate]:
    candidates: list[FileCandidate] = []
    for item in source_index:
        symbol_hits = [
            symbol["name"]
            for symbol in item["symbols"]
            if symbol["name"] in evidence_names or any(symbol["name"].endswith(f"_{name}") for name in evidence_names)
        ]
        text_hits = [name for name in evidence_names if name and name in item["text"]]
        if not symbol_hits and not text_hits:
            continue
        confidence = "high" if symbol_hits else "medium"
        symbols = sorted(set(symbol_hits or text_hits))
        reason = "matched Python function/class names from trace evidence" if symbol_hits else "matched trace span names in source text"
        candidates.append(FileCandidate(path=item["path"], symbols=symbols, confidence=confidence, reason=reason))
    return candidates[:8]


def _select_framework(frameworks: list[str], files: list[FileCandidate]) -> str:
    if "langgraph" in frameworks and any("langgraph" in file.path.lower() or "graph" in file.path.lower() for file in files):
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
    return {
        "risk": "medium",
        "before_pattern": "Trace shows a patchable optimization finding.",
        "proposed_rewrite": "Apply the rewrite hinted by the finding, then validate with replay.",
    }


def _parallelize_template(framework: str) -> dict[str, str]:
    if framework == "langgraph":
        rewrite = "Fan out independent work with parallel LangGraph branches or Send/map nodes, then join before synthesis."
    elif framework == "openai_agents":
        rewrite = "Run independent tool calls with asyncio.gather inside the agent tool orchestration step before returning combined results."
    else:
        rewrite = "Replace serial independent tool calls with asyncio.gather for async tools or ThreadPoolExecutor for sync tools."
    return {
        "risk": "medium",
        "before_pattern": "Three or more same-name tool calls appear serial and independent in the trace.",
        "proposed_rewrite": rewrite,
    }


def _cache_template(framework: str) -> dict[str, str]:
    if framework == "openai_agents":
        rewrite = "Move stable instructions into agent instructions or a cached prompt prefix; pass only run-specific inputs per step."
    else:
        rewrite = "Hoist repeated stable prompt/context into a constant, cached prefix, summary artifact, or provider prompt-cache boundary."
    return {
        "risk": "low",
        "before_pattern": "Multiple model calls resend the same stable context or instruction prefix.",
        "proposed_rewrite": rewrite,
    }


def _schema_template(framework: str) -> dict[str, str]:
    if framework == "openai_agents":
        rewrite = "Use structured outputs or output_type validation, then repair invalid output with a cheap correction step before a full retry."
    else:
        rewrite = "Add JSON schema or Pydantic validation plus a small repair prompt before rerunning the full model step.",
    return {
        "risk": "medium",
        "before_pattern": "Retry spans indicate invalid or unusable model/tool output caused expensive reruns.",
        "proposed_rewrite": rewrite[0] if isinstance(rewrite, tuple) else rewrite,
    }


def _notes(finding_type: str, files: list[FileCandidate]) -> list[str]:
    notes = ["Dry-run only: no files were modified."]
    if not files:
        notes.append("Add explicit span names that match source function names to improve file targeting.")
    if finding_type == "parallelize_tools":
        notes.append("Only parallelize calls that do not mutate shared state and do not depend on each other's outputs.")
    if finding_type == "cache_context":
        notes.append("Confirm cached context has the same invalidation boundary as the original prompt.")
    if finding_type == "add_schema_validation":
        notes.append("Keep the repair path cheaper than a full retry and cap repair attempts.")
    return notes


def _unsupported_finding(finding: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "finding_id": finding.get("finding_id"),
        "type": finding.get("type"),
        "title": finding.get("title"),
        "reason": reason,
    }
