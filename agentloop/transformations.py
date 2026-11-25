"""A constrained registry of executable transformations (issue #398).

A *pass* turns one explicit request into a reviewable change, or abstains and
says why. Every pass declares its contract up front: what it applies to, the
evidence it needs, the effects it tolerates, the resource it aims to reduce,
how to validate it and how to roll it back.

The default is a **dry run**: :func:`propose` reads the checkout, writes
nothing, and returns a candidate with a unified diff and a deterministic
receipt. :func:`execute_candidate` is opt-in. It applies the candidate in a
throwaway ``git worktree`` created from ``HEAD``, runs a host-supplied
validation command there without a shell, and removes the worktree. The
original checkout is never written, and the function checks that afterwards.

Two passes are built in:

``config.json_set``
    Change one value in a JSON file inside the checkout, identified by a JSON
    pointer, but only if the current value is the one the request expects.
``ast.memoize_pure_function``
    Add ``@functools.cache`` to one module-level function. Requires a source
    receipt that resolves exactly (see ``agentloop.source_provenance``) and a
    declared ``effect: "none"`` with ``deterministic: true`` on the traced
    operation (see ``agentloop.execution_ir``).

Neither pass edits anything it can't identify exactly, and neither deploys.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import shutil
import subprocess  # nosec B404 - git and host-supplied argv only, never a shell
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentloop.execution_ir import build_execution_ir
from agentloop.source_provenance import (
    SOURCE_KEY,
    SourceIndex,
    _safe_relative_path,
    resolve_source,
    text_digest,
)

SCHEMA = "agentloop.transformation/v1"
EXECUTION_SCHEMA = "agentloop.transformation_execution/v1"
MAX_FILE_BYTES = 2_000_000


class TransformationError(ValueError):
    """A request or registry operation is malformed; nothing was changed."""


@dataclass(frozen=True)
class PassSpec:
    """The contract every transformation pass declares."""

    pass_id: str
    version: str
    kind: str  # "config" or "ast"
    summary: str
    applies_to: frozenset[str]
    required_evidence: tuple[str, ...]
    allowed_effects: frozenset[str]
    objective: str
    validation: str
    rollback: str
    propose: Callable[[Path, Mapping[str, Any], Any], dict[str, Any]] = field(repr=False)

    def contract(self) -> dict[str, Any]:
        return {
            "id": self.pass_id,
            "version": self.version,
            "kind": self.kind,
            "summary": self.summary,
            "applies_to": sorted(self.applies_to),
            "required_evidence": list(self.required_evidence),
            "allowed_effects": sorted(self.allowed_effects),
            "objective": self.objective,
            "validation": self.validation,
            "rollback": self.rollback,
        }


class PassRegistry:
    """Passes by id. Registering the same id twice is an error, never a silent override."""

    def __init__(self, passes: Sequence[PassSpec] = ()) -> None:
        self._passes: dict[str, PassSpec] = {}
        for spec in passes:
            self.register(spec)

    def register(self, spec: PassSpec) -> None:
        if not isinstance(spec, PassSpec):
            raise TransformationError("only PassSpec instances can be registered")
        if spec.kind not in {"config", "ast"}:
            raise TransformationError("a pass kind must be 'config' or 'ast'")
        if not spec.pass_id or not spec.version:
            raise TransformationError("a pass needs an id and a version")
        if spec.pass_id in self._passes:
            raise TransformationError(f"pass {spec.pass_id!r} is already registered")
        self._passes[spec.pass_id] = spec

    def get(self, pass_id: str) -> PassSpec:
        try:
            return self._passes[pass_id]
        except KeyError:
            raise TransformationError(f"unknown pass {pass_id!r}") from None

    def contracts(self) -> list[dict[str, Any]]:
        return [self._passes[key].contract() for key in sorted(self._passes)]


# Receipts ----------------------------------------------------------------------


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _receipt(spec: PassSpec, request: Mapping[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    candidate = {
        "schema": SCHEMA,
        "pass": {"id": spec.pass_id, "version": spec.version, "kind": spec.kind},
        "request": json.loads(_canonical(request)),
        "objective": spec.objective,
        "validation": spec.validation,
        "rollback": spec.rollback,
        "dry_run": True,
        **body,
    }
    candidate["receipt_sha256"] = _sha(_canonical(candidate))
    return candidate


def _abstain(reasons: list[str]) -> dict[str, Any]:
    return {"status": "abstained", "reasons": reasons, "changes": [], "diff": ""}


def _change(path: str, before: str, after: str) -> dict[str, Any]:
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    changed = _changed_lines(before, after)
    return {
        "path": path,
        "before_sha256": text_digest(before),
        "after_sha256": text_digest(after),
        "changed_lines": changed,
        "diff": diff,
        "_after": after,
    }


def _changed_lines(before: str, after: str) -> list[int]:
    """Lines of the original file touched by the change; insertions attach to the next line."""
    matcher = difflib.SequenceMatcher(a=before.splitlines(), b=after.splitlines(), autojunk=False)
    lines: set[int] = set()
    for tag, a_start, a_end, _, _ in matcher.get_opcodes():
        if tag == "equal":
            continue
        if a_start == a_end:
            lines.add(a_start + 1)
        else:
            lines.update(range(a_start + 1, a_end + 1))
    return sorted(lines)


def _proposed(changes: list[dict[str, Any]], evidence: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "proposed",
        "reasons": [],
        "evidence": evidence,
        "changes": [{key: value for key, value in c.items() if key != "_after"} for c in changes],
        "diff": "".join(change["diff"] for change in changes),
        "_after": {change["path"]: change["_after"] for change in changes},
    }


# Reading the checkout safely ------------------------------------------------------


def _checkout_file(repo: Path, relative: Any) -> tuple[str, Path] | None:
    """A file inside ``repo`` named by a safe relative path, following no escape."""
    path = _safe_relative_path(relative)
    if path is None:
        return None
    root = os.path.realpath(repo)
    target = os.path.realpath(os.path.join(root, *path.split("/")))
    if os.path.commonpath([root, target]) != root or not os.path.isfile(target):
        return None
    return path, Path(target)


def _read(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        with open(path, encoding="utf-8", newline="") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None


# config.json_set ------------------------------------------------------------------


def _pointer(value: Any) -> list[str] | None:
    if not isinstance(value, str) or not value.startswith("/") or len(value) > 512:
        return None
    return [part.replace("~1", "/").replace("~0", "~") for part in value[1:].split("/")]


def _propose_json_set(repo: Path, request: Mapping[str, Any], trace: Any) -> dict[str, Any]:
    for key in ("file", "pointer", "expected", "value"):
        if key not in request:
            return _abstain([f"request is missing {key!r}"])
    located = _checkout_file(repo, request["file"])
    if located is None:
        return _abstain(
            ["file must be an existing file inside the checkout, given as a relative path"]
        )
    relative, path = located
    if not relative.endswith(".json"):
        return _abstain(["config.json_set only edits .json files"])
    text = _read(path)
    if text is None:
        return _abstain(["file is unreadable, not UTF-8 or too large"])
    try:
        document = json.loads(text)
    except ValueError:
        return _abstain(["file is not valid JSON"])
    parts = _pointer(request["pointer"])
    if parts is None:
        return _abstain(["pointer must be a JSON pointer such as /model/name"])
    container: Any = document
    for part in parts[:-1]:
        container = _step(container, part)
        if container is _MISSING:
            return _abstain([f"pointer {request['pointer']!r} does not exist"])
    last = parts[-1]
    current = _step(container, last)
    if current is _MISSING:
        return _abstain([f"pointer {request['pointer']!r} does not exist"])
    if current != request["expected"] or type(current) is not type(request["expected"]):
        return _abstain([f"current value {current!r} is not the expected {request['expected']!r}"])
    if request["value"] == current and type(request["value"]) is type(current):
        return _abstain(["the new value equals the current value"])
    if isinstance(container, list):
        container[int(last)] = request["value"]
    else:
        container[last] = request["value"]
    newline = "\r\n" if "\r\n" in text else "\n"
    indent = _indent(text)
    after = json.dumps(document, indent=indent, ensure_ascii=False).replace("\n", newline)
    if text.endswith(("\n", "\r\n")):
        after += newline
    return _proposed(
        [_change(relative, text, after)],
        {"current_value_matched": True, "pointer": request["pointer"]},
    )


_MISSING = object()


def _step(container: Any, part: str) -> Any:
    if isinstance(container, dict):
        return container.get(part, _MISSING)
    if isinstance(container, list) and part.isdigit() and int(part) < len(container):
        if len(part) > 1 and part.startswith("0"):
            return _MISSING
        return container[int(part)]
    return _MISSING


def _indent(text: str) -> int | None:
    for line in text.splitlines()[1:]:
        stripped = line.lstrip(" ")
        if stripped and len(line) != len(stripped):
            return len(line) - len(stripped)
    return None if "\n" not in text.strip() else 2


# ast.memoize_pure_function -----------------------------------------------------------


_CACHE_NAMES = {"cache", "lru_cache", "cached", "functools.cache", "functools.lru_cache"}


def _propose_memoize(repo: Path, request: Mapping[str, Any], trace: Any) -> dict[str, Any]:
    span_id = request.get("span_id")
    if not isinstance(span_id, str) or not span_id:
        return _abstain(["request needs the span_id of the traced call"])
    if trace is None:
        return _abstain(["this pass needs the trace that recorded the span"])
    ir = build_execution_ir(trace)
    operation = next((op for op in ir["operations"] if op["id"] == span_id), None)
    if operation is None:
        return _abstain([f"span {span_id!r} is not in the trace"])
    reasons = []
    flagged = [item["reason"] for item in ir["unsupported"] if item["operation"] == span_id]
    reasons.extend(f"span has unsupported evidence: {reason}" for reason in flagged)
    if operation["effect"] != {"value": "none", "basis": "declared", "source": "agentloop.effects"}:
        reasons.append("the operation must declare effect 'none' in agentloop.effects")
    if operation["deterministic"].get("value") is not True or (
        operation["deterministic"]["basis"] != "declared"
    ):
        reasons.append("the operation must declare deterministic: true")
    event = next(e for e in trace.events if e.event_id == span_id)
    receipt = (event.metadata or {}).get(SOURCE_KEY)
    files = _python_files(repo)
    location = resolve_source(receipt, SourceIndex(files))
    if location["status"] != "resolved":
        reasons.append(f"source location is {location['status']}: {location.get('reason', '')}")
    if reasons:
        return _abstain(reasons)
    relative = location["path"]
    text = files[relative]
    qualname = location["qualname"]
    if "." in qualname:
        return _abstain(["only module-level functions can be memoized"])
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _abstain(["the file does not parse"])
    node = next(
        (
            item
            for item in tree.body
            if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
            and item.name == qualname
            and min([item.lineno, *(d.lineno for d in item.decorator_list)])
            == location["line_start"]
        ),
        None,
    )
    if node is None:
        return _abstain(["the resolved definition is not a module-level function"])
    if isinstance(node, ast.AsyncFunctionDef):
        return _abstain(["async functions cannot be memoized with functools.cache"])
    if any(isinstance(n, ast.Yield | ast.YieldFrom) for n in ast.walk(node)):
        return _abstain(["generators cannot be memoized with functools.cache"])
    if any(_decorator_name(d) in _CACHE_NAMES for d in node.decorator_list):
        return _abstain(["the function is already cached"])

    lines = text.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in text else "\n"
    def_line = node.lineno - 1
    indent = lines[def_line][: len(lines[def_line]) - len(lines[def_line].lstrip())]
    lines.insert(def_line, f"{indent}@functools.cache{newline}")
    if not _imports_functools(tree):
        lines.insert(_import_position(tree), f"import functools{newline}")
    after = "".join(lines)
    return _proposed(
        [_change(relative, text, after)],
        {
            "source": {key: location[key] for key in ("status", "path", "qualname", "line_start")},
            "effect": "none (declared)",
            "deterministic": "true (declared)",
        },
    )


def _python_files(repo: Path) -> dict[str, str]:
    from agentloop.patches import _index_source_files

    return {
        item["path"]: item["text"] for item in _index_source_files(repo) if item["suffix"] == ".py"
    }


def _decorator_name(node: ast.expr) -> str:
    target = node.func if isinstance(node, ast.Call) else node
    return ast.unparse(target)


def _imports_functools(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.Import)
        and any(alias.name == "functools" and alias.asname is None for alias in node.names)
        for node in tree.body
    )


def _import_position(tree: ast.Module) -> int:
    """After the module docstring and ``from __future__`` imports, as a 0-based line index."""
    position = 0
    for node in tree.body:
        is_docstring = (
            position == 0
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
        is_future = isinstance(node, ast.ImportFrom) and node.module == "__future__"
        if is_docstring or is_future:
            position = node.end_lineno or node.lineno
            continue
        break
    return position


# Registry -----------------------------------------------------------------------------


JSON_SET = PassSpec(
    pass_id="config.json_set",  # nosec B106 - a pass identifier, not a password
    version="1",
    kind="config",
    summary="Change one value in a JSON config file when it still has the expected value.",
    applies_to=frozenset({"route_to_smaller_model", "host_request"}),
    required_evidence=("file inside the checkout", "current value equals the expected value"),
    allowed_effects=frozenset({"none", "read", "write", "external"}),
    objective="the host's stated objective for the setting",
    validation="run the host's validation command and replay quality gates on the candidate",
    rollback="discard the candidate worktree, or revert the one-value change",
    propose=_propose_json_set,
)

MEMOIZE = PassSpec(
    pass_id="ast.memoize_pure_function",  # nosec B106 - a pass identifier, not a password
    version="1",
    kind="ast",
    summary="Add @functools.cache to a declared-pure, module-level function.",
    applies_to=frozenset({"parallelize_tools", "cache_context"}),
    required_evidence=(
        "source receipt resolved exactly",
        "declared effect none",
        "declared deterministic",
    ),
    allowed_effects=frozenset({"none"}),
    objective="latency and repeated calls",
    validation="run the host's validation command; compare replayed outputs before and after",
    rollback="discard the candidate worktree, or delete the inserted decorator line",
    propose=_propose_memoize,
)


def default_registry() -> PassRegistry:
    return PassRegistry([JSON_SET, MEMOIZE])


def propose(
    registry: PassRegistry,
    pass_id: str,
    request: Mapping[str, Any],
    *,
    repo: str | os.PathLike[str],
    trace: Any = None,
) -> dict[str, Any]:
    """Dry run: return a candidate or an abstention. Never writes to ``repo``."""
    if not isinstance(request, Mapping):
        raise TransformationError("request must be a mapping")
    try:
        _canonical(request)
    except (TypeError, ValueError):
        raise TransformationError("request must contain only JSON values") from None
    spec = registry.get(pass_id)
    root = Path(repo)
    if not root.is_dir():
        raise TransformationError("repo must be an existing directory")
    body = spec.propose(root, request, trace)
    after = body.pop("_after", {})
    candidate = _receipt(spec, request, body)
    if after:
        # Kept out of the receipt and JSON output; used only to apply the change.
        candidate["_after"] = after
    return candidate


def conflicts(candidates: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Pairs of proposed candidates that change overlapping lines of the same file."""
    found = []
    proposed = [c for c in candidates if c.get("status") == "proposed"]
    for index, first in enumerate(proposed):
        for second in proposed[index + 1 :]:
            for a in first["changes"]:
                for b in second["changes"]:
                    if a["path"] != b["path"]:
                        continue
                    lines_a, lines_b = set(a["changed_lines"]), set(b["changed_lines"])
                    near = {line + delta for line in lines_a for delta in (-1, 0, 1)}
                    if near & lines_b or a["before_sha256"] != b["before_sha256"]:
                        found.append(
                            {
                                "path": a["path"],
                                "candidates": [first["receipt_sha256"], second["receipt_sha256"]],
                                "reason": "the candidates change the same or adjacent lines"
                                if near & lines_b
                                else "the candidates were proposed against different file contents",
                            }
                        )
    return found


# Opt-in execution ---------------------------------------------------------------------


def _git(args: list[str], cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    if git is None:
        raise TransformationError("git is required to execute a candidate")
    return subprocess.run(  # nosec B603 - fixed git argv, no shell
        [git, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def execute_candidate(
    candidate: Mapping[str, Any],
    *,
    repo: str | os.PathLike[str],
    validation: Sequence[str],
    allow_execution: bool = False,
    timeout: float = 300,
    registry: PassRegistry | None = None,
    trace: Any = None,
) -> dict[str, Any]:
    """Apply a proposed candidate in a temporary worktree of ``HEAD`` and validate it.

    ``allow_execution=True`` is required. ``validation`` is an argv list run in
    the worktree without a shell; exit code 0 accepts the candidate. The pass is
    re-run against the worktree, and the candidate is rejected unless it
    reproduces the same receipt, which catches a checkout whose files differ
    from what was proposed. The worktree is always removed, and the original
    checkout's status is compared before and after.
    """
    if allow_execution is not True:
        raise TransformationError("execution is opt-in: pass allow_execution=True")
    if candidate.get("status") != "proposed":
        raise TransformationError("only a proposed candidate can be executed")
    if (
        not isinstance(validation, Sequence)
        or isinstance(validation, str)
        or not validation
        or not all(isinstance(part, str) and part for part in validation)
    ):
        raise TransformationError("validation must be a non-empty argv list of strings")
    if not 0 < timeout <= 3600:
        raise TransformationError("timeout must be between 0 and 3600 seconds")
    registry = registry or default_registry()
    root = Path(repo).resolve()
    inside = _git(["rev-parse", "--show-toplevel"], root)
    if inside.returncode != 0:
        raise TransformationError("repo must be a git checkout")
    status_before = _git(["status", "--porcelain=v1", "--untracked-files=all"], root).stdout
    head = _git(["rev-parse", "HEAD"], root).stdout.strip()

    result: dict[str, Any] = {
        "schema": EXECUTION_SCHEMA,
        "candidate_receipt_sha256": candidate["receipt_sha256"],
        "pass": candidate["pass"],
        "base_revision": head,
        "validation": list(validation),
    }
    workdir = Path(tempfile.mkdtemp(prefix="agentloop-candidate-"))
    worktree = workdir / "checkout"
    added = _git(["worktree", "add", "--detach", str(worktree), "HEAD"], root)
    try:
        if added.returncode != 0:
            result.update({"status": "error", "reason": "could not create a worktree"})
            return result
        replay = propose(
            registry, candidate["pass"]["id"], candidate["request"], repo=worktree, trace=trace
        )
        if replay.get("receipt_sha256") != candidate["receipt_sha256"]:
            result.update(
                {
                    "status": "rejected",
                    "reason": "the committed source differs from the proposed one; "
                    "commit or re-propose against HEAD",
                    "replay_status": replay.get("status"),
                    "replay_reasons": replay.get("reasons", []),
                }
            )
            return result
        for relative, text in replay.get("_after", {}).items():
            located = _checkout_file(worktree, relative)
            if located is None:
                result.update({"status": "rejected", "reason": f"unsafe path {relative!r}"})
                return result
            with open(located[1], "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
        diff = _git(["diff", "--no-color"], worktree).stdout
        outcome = _run_validation(list(validation), worktree, timeout)
        result.update({"diff": diff, **outcome})
        return result
    finally:
        _git(["worktree", "remove", "--force", str(worktree)], root)
        _git(["worktree", "prune"], root)
        shutil.rmtree(workdir, ignore_errors=True)
        status_after = _git(["status", "--porcelain=v1", "--untracked-files=all"], root).stdout
        result["original_checkout_unchanged"] = status_after == status_before
        result["rolled_back"] = not worktree.exists()


def _run_validation(argv: list[str], cwd: Path, timeout: float) -> dict[str, Any]:
    try:
        completed = subprocess.run(  # nosec B603 - host-supplied argv, no shell
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "rejected", "reason": f"validation exceeded {timeout:g} seconds"}
    except OSError as error:
        return {"status": "error", "reason": f"validation could not start ({type(error).__name__})"}
    accepted = completed.returncode == 0
    return {
        "status": "accepted" if accepted else "rejected",
        "reason": None if accepted else f"validation exited with code {completed.returncode}",
        "exit_code": completed.returncode,
        "validation_output_tail": (completed.stdout + completed.stderr)[-2000:],
    }


def candidate_json(candidate: Mapping[str, Any]) -> str:
    """The candidate as canonical JSON, without the internal file contents."""
    return _canonical({key: value for key, value in candidate.items() if key != "_after"})


__all__ = [
    "EXECUTION_SCHEMA",
    "JSON_SET",
    "MEMOIZE",
    "SCHEMA",
    "PassRegistry",
    "PassSpec",
    "TransformationError",
    "candidate_json",
    "conflicts",
    "default_registry",
    "execute_candidate",
    "propose",
]
