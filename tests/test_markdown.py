from __future__ import annotations

import pytest

from agentloop.markdown import (
    markdown_code_span,
    markdown_fenced_code,
    markdown_heading,
    markdown_table_cell,
    markdown_text,
)


def test_plain_text_and_heading_golden_values() -> None:
    value = "safe\r\n## injected\n<script>[link] *bold* _em_ ~del~ \\ 雪"
    expected = (
        "safe ## injected &lt;script&gt;&#91;link&#93; &#42;bold&#42; "
        "&#95;em&#95; &#126;del&#126; &#92; 雪"
    )

    assert markdown_text(value) == expected
    assert markdown_heading(value) == expected
    assert markdown_text(None) == ""
    assert markdown_text("latency_regression") == "latency_regression"
    assert "\n" not in markdown_heading(value)


def test_table_cell_golden_value_preserves_one_column() -> None:
    value = (
        "pipe|cr\rline\nnext\\slash `one` ``two`` ```three``` <tag> [link] *em* _under_ ~strike~ 雪"
    )

    assert markdown_table_cell(value) == (
        "pipe&#124;cr line next&#92;slash &#96;one&#96; &#96;&#96;two&#96;&#96; "
        "&#96;&#96;&#96;three&#96;&#96;&#96; &lt;tag&gt; &#91;link&#93; "
        "&#42;em&#42; &#95;under&#95; &#126;strike&#126; 雪"
    )
    assert markdown_table_cell("") == ""
    assert markdown_table_cell("cache_context") == "cache_context"
    assert "|" not in markdown_table_cell(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("plain", "`plain`"),
        ("`one`", "`` `one` ``"),
        ("a `` b", "```a `` b```"),
        ("```", "```` ``` ````"),
        (" edge ", "`  edge  `"),
        ("line\r\nbreak", "`line break`"),
        ("雪", "`雪`"),
        ("", ""),
    ],
)
def test_code_span_uses_safe_golden_delimiters(value: str, expected: str) -> None:
    assert markdown_code_span(value) == expected


def test_fenced_code_uses_a_longer_fence_and_normalizes_line_endings() -> None:
    value = "before\r\n```\n````\nafter"

    assert markdown_fenced_code(value, language="text") == (
        "`````text\nbefore\n```\n````\nafter\n`````"
    )
    assert markdown_fenced_code("", language="text") == "```text\n\n```"


def test_fenced_code_rejects_untrusted_info_strings() -> None:
    with pytest.raises(ValueError, match="unsafe characters"):
        markdown_fenced_code("content", language="text`\n## injected")
