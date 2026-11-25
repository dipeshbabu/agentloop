"""Trace-to-source provenance (issue #395): capture, redaction, resolution, planning."""

from __future__ import annotations

import asyncio
import importlib.util
import itertools
import json
import os
import sys
from pathlib import Path

import pytest

from agentloop.decorators import trace_tool, traceable
from agentloop.otel import trace_from_otel, trace_to_otel
from agentloop.patches import build_patch_plan, patch_plan_to_markdown
from agentloop.source_provenance import (
    CAPTURE_ENV,
    SOURCE_KEY,
    SourceIndex,
    capture_enabled,
    capture_source,
    python_definitions,
    receipt_from_metadata,
    resolve_source,
    text_digest,
)
from agentloop.tracer import AgentTrace, trace_agent, trace_tool_call

_counter = itertools.count()

TOOLS = """\
import functools

from agentloop.decorators import traceable


def logged(func):
    @functools.wraps(func)
    def inner(*args, **kwargs):
        return func(*args, **kwargs)

    return inner


class Search:
    @traceable(capture_source=True)
    def lookup(self, query):
        return query


class Archive:
    def lookup(self, query):
        return query


@traceable(capture_source=True)
@logged
def lookup(query):
    return query


@traceable(capture_source=True)
async def fetch(url):
    return url


@traceable(capture_source=True)
def stream(n):
    yield from range(n)


def factory():
    @traceable(capture_source=True)
    def nested(value):
        return value

    return nested
"""

DUPLICATE = """\
def lookup(query):
    return query
"""


def write_repo(root: Path, files: dict[str, str]) -> Path:
    (root / ".git").mkdir(parents=True, exist_ok=True)
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    return root


def load(path: Path):
    name = f"provenance_fixture_{next(_counter)}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repo(tmp_path):
    root = write_repo(
        tmp_path / "repo",
        {"agents/tools.py": TOOLS, "agents/other.py": DUPLICATE, "app/lookup.py": DUPLICATE},
    )
    return root, load(root / "agents/tools.py")


def receipts(trace: AgentTrace) -> list[dict]:
    return [event.metadata.get(SOURCE_KEY) for event in trace.events]


def index_of(root: Path) -> SourceIndex:
    files = {}
    for path in root.rglob("*.py"):
        files[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
    return SourceIndex(files)


# Capture -----------------------------------------------------------------------


def test_capture_is_off_by_default(monkeypatch):
    monkeypatch.delenv(CAPTURE_ENV, raising=False)

    @traceable
    def plain(value):
        return value

    with trace_agent("off") as trace:
        plain(1)
    assert SOURCE_KEY not in trace.events[0].metadata
    assert capture_enabled() is False
    assert capture_enabled(True) is True


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_environment_flag_enables_capture(monkeypatch, value):
    monkeypatch.setenv(CAPTURE_ENV, value)
    assert capture_enabled() is True
    assert capture_enabled(False) is False


def test_receipt_has_relative_path_qualname_lines_and_digest(repo):
    root, tools = repo
    with trace_agent("capture") as trace:
        tools.Search().lookup("q")
    (receipt,) = receipts(trace)
    text = (root / "agents/tools.py").read_text(encoding="utf-8")
    assert receipt["schema"] == "agentloop.source/v1"
    assert receipt["status"] == "captured"
    assert receipt["path"] == "agents/tools.py"
    assert receipt["qualname"] == "Search.lookup"
    assert receipt["content_sha256"] == text_digest(text)
    lines = text.splitlines()
    # line_start includes the decorator line.
    assert lines[receipt["line_start"] - 1].strip() == "@traceable(capture_source=True)"
    assert lines[receipt["line_end"] - 1].strip() == "return query"


def test_receipt_never_contains_absolute_paths_or_source(repo):
    root, tools = repo
    with trace_agent("redaction") as trace:
        tools.lookup("q")
        asyncio.run(tools.fetch("u"))
        list(tools.stream(2))
        tools.factory()(1)
    serialized = json.dumps(trace.to_dict())
    assert str(root) not in serialized
    assert str(root).replace("\\", "/") not in serialized
    assert "return query" not in serialized
    for receipt in receipts(trace):
        assert not os.path.isabs(receipt["path"])
        assert set(receipt) <= {
            "schema", "status", "path", "qualname", "module",
            "line_start", "line_end", "content_sha256", "revision",
        }  # fmt: skip


def test_stacked_decorators_async_generators_and_nested_functions(repo):
    _, tools = repo
    with trace_agent("shapes") as trace:
        tools.lookup("q")
        asyncio.run(tools.fetch("u"))
        assert list(tools.stream(3)) == [0, 1, 2]
        tools.factory()(1)
    names = [receipt["qualname"] for receipt in receipts(trace)]
    # functools.wraps is unwrapped to the real definition, not the inner wrapper.
    assert names == ["lookup", "fetch", "stream", "factory.<locals>.nested"]
    assert all(receipt["status"] == "captured" for receipt in receipts(trace))


def test_receipt_is_copied_per_span(repo):
    _, tools = repo
    with trace_agent("copies") as trace:
        tools.lookup("a")
        tools.lookup("b")
    first, second = receipts(trace)
    first["qualname"] = "edited"
    assert second["qualname"] == "lookup"


def test_outside_root_and_unlocatable_functions_have_no_path(tmp_path, monkeypatch):
    outside = write_repo(tmp_path / "elsewhere", {"mod.py": DUPLICATE})
    module = load(outside / "mod.py")
    receipt = capture_source(module.lookup, root=tmp_path / "unrelated")
    assert receipt["status"] == "outside_root"
    assert "path" not in receipt and receipt["qualname"] == "lookup"

    namespace: dict = {}
    exec("def generated():\n    return 1\n", namespace)  # noqa: S102 - code with no file
    assert capture_source(namespace["generated"])["status"] == "unavailable"
    assert capture_source(len)["status"] == "unavailable"
    assert capture_source(lambda: None)["qualname"].endswith("<lambda>")


def test_root_and_revision_can_be_configured(tmp_path, monkeypatch):
    root = write_repo(tmp_path / "configured", {"pkg/mod.py": DUPLICATE})
    module = load(root / "pkg/mod.py")
    monkeypatch.setenv("AGENTLOOP_SOURCE_ROOT", str(root / "pkg"))
    monkeypatch.setenv("AGENTLOOP_SOURCE_REVISION", "abc123")
    receipt = capture_source(module.lookup)
    assert receipt["path"] == "mod.py"
    assert receipt["revision"] == "abc123"


def test_crlf_checkout_has_the_same_digest():
    assert text_digest("a\r\nb\r\n") == text_digest("a\nb\n") == text_digest("a\rb\r")


# Resolution --------------------------------------------------------------------


def capture_lookup(repo) -> tuple[Path, dict]:
    root, tools = repo
    with trace_agent("resolve") as trace:
        tools.Search().lookup("q")
    return root, receipts(trace)[0]


def test_exact_match_resolves(repo):
    root, receipt = capture_lookup(repo)
    location = resolve_source(receipt, index_of(root))
    assert location["status"] == "resolved"
    assert location["path"] == "agents/tools.py"
    assert location["qualname"] == "Search.lookup"


def test_duplicate_symbol_names_do_not_confuse_resolution(repo):
    # Search.lookup, Archive.lookup, module-level lookup and two other files'
    # lookup all share a name; only the recorded qualified name matches.
    root, receipt = capture_lookup(repo)
    location = resolve_source(receipt, index_of(root))
    definitions = python_definitions((root / "agents/tools.py").read_text(encoding="utf-8"))
    assert (location["line_start"], location["line_end"]) == definitions["Search.lookup"][0]
    assert {"lookup", "Search.lookup", "Archive.lookup"} <= set(definitions)


def test_changed_file_is_stale_and_not_eligible(repo):
    root, receipt = capture_lookup(repo)
    path = root / "agents/tools.py"
    path.write_text("# a new first line\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    location = resolve_source(receipt, index_of(root))
    assert location["status"] == "stale"
    # Still points a reviewer at the current definition.
    assert location["path"] == "agents/tools.py"
    assert location["line_start"] == receipt["line_start"] + 1


def test_moved_file_is_found_by_digest(repo):
    root, receipt = capture_lookup(repo)
    (root / "agents/tools.py").rename(root / "agents/renamed.py")
    location = resolve_source(receipt, index_of(root))
    assert location["status"] == "moved"
    assert location["path"] == "agents/renamed.py"
    assert location["recorded_path"] == "agents/tools.py"


def test_identical_copies_are_ambiguous(repo):
    root, receipt = capture_lookup(repo)
    text = (root / "agents/tools.py").read_text(encoding="utf-8")
    (root / "agents/tools.py").unlink()
    (root / "copy_a.py").write_text(text, encoding="utf-8")
    (root / "copy_b.py").write_text(text, encoding="utf-8")
    location = resolve_source(receipt, index_of(root))
    assert location["status"] == "ambiguous"
    assert location["candidates"] == ["copy_a.py", "copy_b.py"]


def test_missing_file_and_missing_symbol(repo):
    root, receipt = capture_lookup(repo)
    (root / "agents/tools.py").unlink()
    assert resolve_source(receipt, index_of(root))["status"] == "file_missing"
    root2, receipt2 = capture_lookup(repo)
    renamed = {**receipt2, "qualname": "Search.find"}
    write_repo(root2, {"agents/tools.py": TOOLS})
    assert resolve_source(renamed, index_of(root2))["status"] == "symbol_missing"


def test_same_name_defined_twice_in_one_scope_needs_lines():
    text = "if FLAG:\n    def pick():\n        return 1\nelse:\n    def pick():\n        return 2\n"
    index = SourceIndex({"m.py": text})
    digest = text_digest(text)
    base = {"schema": "agentloop.source/v1", "status": "captured", "path": "m.py"}
    second = {**base, "qualname": "pick", "line_start": 5, "line_end": 6, "content_sha256": digest}
    assert resolve_source(second, index)["line_start"] == 5
    assert resolve_source(second, index)["status"] == "resolved"
    no_lines = {**base, "qualname": "pick", "content_sha256": digest}
    assert resolve_source(no_lines, index)["status"] == "ambiguous"


@pytest.mark.parametrize(
    "receipt",
    [
        {"path": "/etc/passwd", "qualname": "x"},
        {"path": "../outside.py", "qualname": "x"},
        {"path": "a/../../b.py", "qualname": "x"},
        {"path": "C:/Windows/x.py", "qualname": "x"},
        {"path": "a\\b.py", "qualname": "x"},
        {"path": "a.py", "qualname": "x; import os"},
        {"path": "a.py"},
        {"path": 3, "qualname": "x"},
        {"path": "a.py", "qualname": "x", "status": "weird"},
    ],
)
def test_malformed_or_escaping_receipts_are_invalid(receipt):
    index = SourceIndex({"a.py": "def x():\n    pass\n"})
    assert resolve_source(receipt, index)["status"] == "invalid"


def test_missing_receipt_and_recorded_statuses_stay_explicit():
    index = SourceIndex({})
    assert resolve_source(None, index)["status"] == "unavailable"
    assert resolve_source({"status": "outside_root", "qualname": "f"}, index)["status"] == (
        "outside_root"
    )
    assert resolve_source({"status": "unavailable"}, index)["status"] == "unavailable"


def test_receipt_without_digest_is_unverified():
    index = SourceIndex({"a.py": "def x():\n    pass\n"})
    location = resolve_source({"path": "a.py", "qualname": "x", "line_start": 1}, index)
    assert location["status"] == "unverified"
    assert location["path"] == "a.py"


def test_unparseable_source_has_no_definitions():
    assert python_definitions("def broken(:\n") == {}
    assert python_definitions("x = '\0'") == {}


# Trace formats -----------------------------------------------------------------


def test_receipt_survives_json_and_otlp_round_trips(repo, tmp_path):
    root, tools = repo
    with trace_agent("formats") as trace:
        tools.Search().lookup("q")
    path = trace.export_json(tmp_path / "trace.json")
    from_json = AgentTrace.from_json(path)
    assert receipts(from_json) == receipts(trace)
    round_tripped = trace_from_otel(trace_to_otel(trace))
    assert round_tripped.events[0].metadata[SOURCE_KEY] == receipts(trace)[0]


def test_otel_code_attributes_are_read_as_unverified():
    receipt = receipt_from_metadata(
        {"code.file.path": "a.py", "code.function.name": "x", "code.line.number": 1}
    )
    assert receipt == {"schema": "otel.code", "status": "captured", "path": "a.py",
                       "qualname": "x", "line_start": 1}  # fmt: skip
    legacy = receipt_from_metadata(
        {"code.filepath": "a.py", "code.function": "x", "code.namespace": "C"}
    )
    assert legacy["qualname"] == "C.x"
    assert receipt_from_metadata({"code.filepath": "/abs/a.py"})["path"] == "/abs/a.py"
    index = SourceIndex({"a.py": "def x():\n    pass\n"})
    assert resolve_source(receipt, index)["status"] == "unverified"
    assert receipt_from_metadata({}) is None
    assert receipt_from_metadata(None) is None


# Patch planning ----------------------------------------------------------------


def parallel_trace(tools, *, source: bool) -> AgentTrace:
    with trace_agent("plan") as trace:
        for _ in range(3):
            if source:
                tools.lookup("q")
            else:
                with trace_tool_call("lookup", metadata={"parallel_safe": True}):
                    pass
    for event in trace.events:
        event.metadata["parallel_safe"] = True
    return trace


def plan_for(trace, root):
    plan = build_patch_plan(trace, repo_path=root, allowed_root=root.parent)
    (item,) = [p for p in plan["patch_plans"] if p["type"] == "parallelize_tools"]
    return plan, item


def test_patch_plan_targets_the_resolved_definition_first(repo):
    root, tools = repo
    plan, item = plan_for(parallel_trace(tools, source=True), root)
    first = item["files"][0]
    assert first["path"] == "agents/tools.py"
    assert first["confidence"] == "exact"
    assert first["rewrite_eligible"] is True
    assert first["provenance"] == "resolved"
    assert item["rewrite_eligible"] is True
    assert [loc["status"] for loc in item["source_locations"]] == ["resolved"]
    assert len(item["source_locations"][0]["span_ids"]) == 3
    # Same-named functions elsewhere remain leads, never rewrite targets.
    others = [f for f in item["files"][1:]]
    assert {f["path"] for f in others} >= {"agents/other.py", "app/lookup.py"}
    assert not any(f["rewrite_eligible"] for f in others)
    assert plan["summary"]["source_provenance"] == {"resolved": 3}
    markdown = patch_plan_to_markdown(plan)
    assert "Rewrite eligible: yes" in markdown and "resolved" in markdown


def test_legacy_traces_plan_as_before_and_are_never_eligible(repo):
    root, tools = repo
    plan, item = plan_for(parallel_trace(tools, source=False), root)
    assert item["files"], "name matching still finds leads"
    assert not any(f["rewrite_eligible"] for f in item["files"])
    assert all("provenance" not in f for f in item["files"])
    assert item["rewrite_eligible"] is False
    assert {loc["status"] for loc in item["source_locations"]} == {"unavailable"}
    assert "Rewrite eligible: no" in patch_plan_to_markdown(plan)


def test_stale_receipts_demote_the_file(repo):
    root, tools = repo
    trace = parallel_trace(tools, source=True)
    path = root / "agents/tools.py"
    path.write_text(path.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    _, item = plan_for(trace, root)
    first = item["files"][0]
    assert (first["path"], first["provenance"], first["rewrite_eligible"]) == (
        "agents/tools.py",
        "stale",
        False,
    )
    assert item["rewrite_eligible"] is False


def test_decorator_aliases_accept_capture_source(repo):
    root, _ = repo

    @trace_tool(capture_source=True)
    def aliased():
        return 1

    with trace_agent("alias") as trace:
        aliased()
    assert receipts(trace)[0]["qualname"].endswith("aliased")
