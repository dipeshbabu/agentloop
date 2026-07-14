from __future__ import annotations

import re

from agentloop.exporters import export_report_markdown
from agentloop.markdown import code_span, escape_cell, escape_inline
from agentloop.tracer import trace_agent, trace_model_call

# --- escape_inline -----------------------------------------------------------

def test_escape_inline_neutralizes_heading_injection() -> None:
    out = escape_inline("value\n## Injected heading")
    assert "\n" not in out
    assert "## Injected heading" not in out
    # The '#' is escaped so it can't open a heading.
    assert "\\#\\# Injected heading" in out


def test_escape_inline_escapes_table_pipe() -> None:
    assert escape_inline("a | b") == "a \\| b"


def test_escape_inline_escapes_inline_markup() -> None:
    out = escape_inline("use `backticks` and *stars* and _score_")
    assert "`" not in out.replace("\\`", "")
    assert "\\`backticks\\`" in out
    assert "\\*stars\\*" in out


def test_escape_inline_entity_encodes_raw_html() -> None:
    out = escape_inline("<script>alert(1)</script> & co")
    assert "<" not in out and ">" not in out
    assert "&lt;" in out and "&gt;" in out and "&amp;" in out


def test_escape_inline_does_not_double_escape_backslash() -> None:
    # A single backslash becomes exactly one escaped backslash, not two.
    assert escape_inline("a\\b") == "a\\\\b"


# --- code_span ---------------------------------------------------------------

def test_code_span_widens_fence_past_inner_backticks() -> None:
    span = code_span("has ` one and `` two")
    # Fence must be longer than the longest inner run (2) -> 3 backticks.
    assert span.startswith("```") and span.endswith("```")


def test_code_span_pads_when_content_edges_are_backticks() -> None:
    span = code_span("`x`")
    assert span.startswith("`` ") and span.endswith(" ``")


def test_code_span_collapses_newlines() -> None:
    assert "\n" not in code_span("a\nb")


# --- escape_cell is the inline escaper ---------------------------------------

def test_escape_cell_is_inline() -> None:
    assert escape_cell("a | b\nc") == escape_inline("a | b\nc")


# --- end-to-end: a malicious trace cannot corrupt the report -----------------

def test_report_markdown_is_injection_safe(tmp_path) -> None:
    evil_name = "lookup | summarize\n## Injected"
    with trace_agent(evil_name) as trace:
        with trace_model_call("call `end` | extra", input_tokens=1, output_tokens=1):
            pass

    out = export_report_markdown(trace.report(), tmp_path / "report.md")
    lines = out.read_text(encoding="utf-8").splitlines()

    # The heading line carries the escaped name and stays a single line; the
    # injected '## Injected' never becomes its own heading.
    heading_lines = [ln for ln in lines if ln.startswith("# AgentLoop Report:")]
    assert len(heading_lines) == 1
    assert not any(ln.strip() == "## Injected" for ln in lines)

    # Every events-table row keeps the fixed 7-column shape despite the raw '|'
    # in the event name: the pipe is escaped as '\|' (a literal, not a column
    # separator), so counting only *unescaped* pipes yields the header's shape.
    unescaped_pipe = re.compile(r"(?<!\\)\|")
    header = "| Type | Name | Duration ms | Model | Input | Output | Status |"
    expected_cols = len(unescaped_pipe.split(header))
    row_prefix = "| model"  # the single model-call event row
    body_rows = [ln for ln in lines if ln.startswith(row_prefix)]
    assert body_rows, "expected an events-table row"
    for row in body_rows:
        assert len(unescaped_pipe.split(row)) == expected_cols, row
