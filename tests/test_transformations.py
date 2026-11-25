"""Constrained transformation registry and isolated candidate execution (issue #398)."""

from __future__ import annotations

import importlib.util
import itertools
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agentloop.execution_ir import EFFECTS_KEY
from agentloop.tracer import trace_agent
from agentloop.transformations import (
    MEMOIZE,
    PassRegistry,
    TransformationError,
    candidate_json,
    conflicts,
    default_registry,
    execute_candidate,
    propose,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
_counter = itertools.count()

MODULE = '''"""Pricing helpers."""

from __future__ import annotations

from agentloop.decorators import traceable


@traceable(capture_source=True)
def price(item):
    return len(item)


@traceable(capture_source=True)
def save(item):
    return item
'''
CONFIG = '{\n  "model": {\n    "name": "large"\n  },\n  "retries": 3\n}\n'


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "agent").mkdir(parents=True)
    (root / "agent/pricing.py").write_text(MODULE, encoding="utf-8", newline="\n")
    (root / "config.json").write_text(CONFIG, encoding="utf-8", newline="\n")
    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    git(root, "config", "core.autocrlf", "false")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "init")
    return root


def load(path: Path):
    name = f"transform_fixture_{next(_counter)}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def traced(repo: Path, function: str, effects: dict | None):
    module = load(repo / "agent/pricing.py")
    with trace_agent("t") as trace:
        getattr(module, function)("abc")
    if effects is not None:
        trace.events[0].metadata[EFFECTS_KEY] = effects
    return trace, trace.events[0].event_id


PURE = {"effect": "none", "deterministic": True}
OK = [sys.executable, "-c", "import sys; sys.exit(0)"]
FAIL = [sys.executable, "-c", "import sys; sys.exit(3)"]


# Registry ------------------------------------------------------------------------


def test_registry_contracts_and_duplicates():
    registry = default_registry()
    ids = [contract["id"] for contract in registry.contracts()]
    assert ids == ["ast.memoize_pure_function", "config.json_set"]
    contract = registry.get("ast.memoize_pure_function").contract()
    assert contract["allowed_effects"] == ["none"]
    assert "rollback" in contract and "validation" in contract
    with pytest.raises(TransformationError):
        registry.register(MEMOIZE)
    with pytest.raises(TransformationError):
        registry.get("nope")
    with pytest.raises(TransformationError):
        PassRegistry(["not a spec"])  # type: ignore[list-item]


# config.json_set -----------------------------------------------------------------


def json_request(**overrides):
    return {"file": "config.json", "pointer": "/model/name", "expected": "large",
            "value": "small", **overrides}  # fmt: skip


def test_json_set_is_a_dry_run_with_a_minimal_diff(repo):
    before = (repo / "config.json").read_bytes()
    candidate = propose(default_registry(), "config.json_set", json_request(), repo=repo)
    assert candidate["status"] == "proposed" and candidate["dry_run"] is True
    assert (repo / "config.json").read_bytes() == before
    assert '-    "name": "large"' in candidate["diff"]
    assert '+    "name": "small"' in candidate["diff"]
    assert candidate["changes"][0]["changed_lines"] == [3]
    assert "_after" not in json.loads(candidate_json(candidate))


@pytest.mark.parametrize(
    ("request_overrides", "fragment"),
    [
        ({"expected": "medium"}, "is not the expected"),
        ({"expected": 3, "pointer": "/retries", "value": 3}, "equals the current"),
        ({"pointer": "/model/missing"}, "does not exist"),
        ({"pointer": "model"}, "JSON pointer"),
        ({"file": "../outside.json"}, "inside the checkout"),
        ({"file": "/etc/passwd"}, "inside the checkout"),
        ({"file": "agent/pricing.py"}, ".json"),
        ({"file": "missing.json"}, "inside the checkout"),
    ],
)
def test_json_set_abstains(repo, request_overrides, fragment):
    candidate = propose(
        default_registry(), "config.json_set", json_request(**request_overrides), repo=repo
    )
    assert candidate["status"] == "abstained"
    assert any(fragment in reason for reason in candidate["reasons"]), candidate["reasons"]


def test_json_set_rejects_type_confusion(repo):
    request = json_request(pointer="/retries", expected=3.0, value=1)
    assert (
        propose(default_registry(), "config.json_set", request, repo=repo)["status"] == "abstained"
    )


def test_requests_must_be_json(repo):
    with pytest.raises(TransformationError):
        propose(default_registry(), "config.json_set", {"file": object()}, repo=repo)


# ast.memoize_pure_function --------------------------------------------------------


def test_memoize_requires_declared_pure_effect(repo):
    trace, span = traced(repo, "save", {"effect": "write", "deterministic": True})
    candidate = propose(
        default_registry(), MEMOIZE.pass_id, {"span_id": span}, repo=repo, trace=trace
    )
    assert candidate["status"] == "abstained"
    assert any("effect 'none'" in reason for reason in candidate["reasons"])
    trace, span = traced(repo, "price", None)
    candidate = propose(
        default_registry(), MEMOIZE.pass_id, {"span_id": span}, repo=repo, trace=trace
    )
    assert {"the operation must declare effect 'none' in agentloop.effects",
            "the operation must declare deterministic: true"} <= set(candidate["reasons"])  # fmt: skip


def test_memoize_inserts_decorator_and_import(repo):
    trace, span = traced(repo, "price", PURE)
    candidate = propose(
        default_registry(), MEMOIZE.pass_id, {"span_id": span}, repo=repo, trace=trace
    )
    assert candidate["status"] == "proposed", candidate["reasons"]
    diff = candidate["diff"]
    assert "+import functools" in diff and "+@functools.cache" in diff
    after = candidate["_after"]["agent/pricing.py"]
    # The import goes after the docstring and __future__ import.
    assert after.index("from __future__") < after.index("import functools")
    # Innermost decorator: the trace decorator still wraps the cached function.
    assert "@traceable(capture_source=True)\n@functools.cache\ndef price" in after
    compile(after, "pricing.py", "exec")


def test_memoize_abstains_on_stale_source(repo):
    trace, span = traced(repo, "price", PURE)
    path = repo / "agent/pricing.py"
    path.write_text(path.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    candidate = propose(
        default_registry(), MEMOIZE.pass_id, {"span_id": span}, repo=repo, trace=trace
    )
    assert candidate["status"] == "abstained"
    assert any("stale" in reason for reason in candidate["reasons"])


def test_memoize_abstains_without_trace_or_span(repo):
    trace, _ = traced(repo, "price", PURE)
    registry = default_registry()
    assert propose(registry, MEMOIZE.pass_id, {"span_id": "x"}, repo=repo)["status"] == "abstained"
    assert propose(registry, MEMOIZE.pass_id, {"span_id": "x"}, repo=repo, trace=trace)[
        "reasons"
    ] == ["span 'x' is not in the trace"]


def test_receipts_are_deterministic_and_location_independent(repo, tmp_path):
    first = propose(default_registry(), "config.json_set", json_request(), repo=repo)
    second = propose(default_registry(), "config.json_set", json_request(), repo=repo)
    copy = tmp_path / "copy"
    shutil.copytree(repo, copy)
    third = propose(default_registry(), "config.json_set", json_request(), repo=copy)
    assert first["receipt_sha256"] == second["receipt_sha256"] == third["receipt_sha256"]
    assert str(repo) not in candidate_json(first)


def test_conflicting_candidates_are_detected(repo):
    registry = default_registry()
    a = propose(registry, "config.json_set", json_request(), repo=repo)
    b = propose(registry, "config.json_set", json_request(value="medium"), repo=repo)
    c = propose(registry, "config.json_set",
                json_request(pointer="/retries", expected=3, value=1), repo=repo)  # fmt: skip
    found = conflicts([a, b, c])
    assert [item["candidates"] for item in found] == [[a["receipt_sha256"], b["receipt_sha256"]]]
    assert conflicts([a, {"status": "abstained"}]) == []


# Execution --------------------------------------------------------------------------


def test_execution_is_opt_in(repo):
    candidate = propose(default_registry(), "config.json_set", json_request(), repo=repo)
    with pytest.raises(TransformationError, match="opt-in"):
        execute_candidate(candidate, repo=repo, validation=OK)
    with pytest.raises(TransformationError):
        execute_candidate(candidate, repo=repo, validation="python -c 1", allow_execution=True)
    abstained = propose(
        default_registry(), "config.json_set", json_request(expected="x"), repo=repo
    )
    with pytest.raises(TransformationError):
        execute_candidate(abstained, repo=repo, validation=OK, allow_execution=True)


def test_accepted_candidate_leaves_the_checkout_untouched(repo):
    before = git(repo, "status", "--porcelain")
    original = (repo / "config.json").read_bytes()
    candidate = propose(default_registry(), "config.json_set", json_request(), repo=repo)
    check = [sys.executable, "-c",
             "import json,sys; sys.exit(json.load(open('config.json'))['model']['name'] != 'small')"]  # fmt: skip
    result = execute_candidate(candidate, repo=repo, validation=check, allow_execution=True)
    assert result["status"] == "accepted", result
    assert '+    "name": "small"' in result["diff"]
    assert result["original_checkout_unchanged"] is True
    assert result["rolled_back"] is True
    assert (repo / "config.json").read_bytes() == original
    assert git(repo, "status", "--porcelain") == before
    assert git(repo, "worktree", "list").count("\n") == 1


def test_failed_validation_rejects_and_is_retained(repo):
    candidate = propose(default_registry(), "config.json_set", json_request(), repo=repo)
    result = execute_candidate(candidate, repo=repo, validation=FAIL, allow_execution=True)
    assert result["status"] == "rejected"
    assert result["exit_code"] == 3
    assert result["candidate_receipt_sha256"] == candidate["receipt_sha256"]
    assert result["rolled_back"] is True


def test_uncommitted_source_is_rejected_not_executed(repo):
    # Proposed against an edited working tree that HEAD does not contain.
    (repo / "config.json").write_text(CONFIG.replace("3", "4"), encoding="utf-8", newline="\n")
    candidate = propose(default_registry(), "config.json_set",
                        json_request(pointer="/retries", expected=4, value=1), repo=repo)  # fmt: skip
    result = execute_candidate(candidate, repo=repo, validation=OK, allow_execution=True)
    assert result["status"] == "rejected"
    assert "differs from the proposed" in result["reason"]
    assert result["original_checkout_unchanged"] is True


def test_memoize_candidate_runs_in_a_worktree(repo):
    trace, span = traced(repo, "price", PURE)
    candidate = propose(
        default_registry(), MEMOIZE.pass_id, {"span_id": span}, repo=repo, trace=trace
    )
    check = [sys.executable, "-c", "import ast; ast.parse(open('agent/pricing.py').read())"]
    result = execute_candidate(candidate, repo=repo, validation=check, allow_execution=True,
                               trace=trace)  # fmt: skip
    assert result["status"] == "accepted", result
    assert "+@functools.cache" in result["diff"]
    assert "functools" not in (repo / "agent/pricing.py").read_text(encoding="utf-8")


def test_validation_timeout_rejects(repo):
    candidate = propose(default_registry(), "config.json_set", json_request(), repo=repo)
    slow = [sys.executable, "-c", "import time; time.sleep(30)"]
    result = execute_candidate(candidate, repo=repo, validation=slow, allow_execution=True,
                               timeout=1)  # fmt: skip
    assert result["status"] == "rejected" and "exceeded" in result["reason"]
    assert result["rolled_back"] is True


def test_execution_requires_a_git_checkout(tmp_path):
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain/config.json").write_text(CONFIG, encoding="utf-8")
    candidate = propose(
        default_registry(), "config.json_set", json_request(), repo=tmp_path / "plain"
    )
    with pytest.raises(TransformationError, match="git checkout"):
        execute_candidate(candidate, repo=tmp_path / "plain", validation=OK, allow_execution=True)
