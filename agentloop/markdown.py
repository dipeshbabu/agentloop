from __future__ import annotations

import re
from typing import Any

_LINE_BREAKS = re.compile(r"\r\n?|\n|\x85|\u2028|\u2029")
_BACKTICK_RUNS = re.compile(r"`+")
_SAFE_INFO_STRING = re.compile(r"[A-Za-z0-9_.+-]*\Z")

# Markdown treats these characters as inline syntax, while HTML parsers give
# angle brackets and ampersands additional meaning. Character references keep
# the rendered text unchanged without relying on escape ordering.
_INLINE_ESCAPES = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\\": "&#92;",
    "`": "&#96;",
    "*": "&#42;",
    "_": "&#95;",
    "[": "&#91;",
    "]": "&#93;",
    "~": "&#126;",
}
_TABLE_TRANSLATION = str.maketrans({"|": "&#124;"})


def markdown_text(value: Any) -> str:
    """Render an untrusted value as single-line Markdown text."""

    content = _single_line(value)
    return "".join(
        character
        if character == "_" and _underscore_is_inside_identifier(content, index)
        else _INLINE_ESCAPES.get(character, character)
        for index, character in enumerate(content)
    )


def markdown_heading(value: Any) -> str:
    """Render an untrusted value inside a heading without allowing new blocks."""

    return markdown_text(value)


def markdown_table_cell(value: Any) -> str:
    """Render an untrusted value without adding Markdown table columns or rows."""

    return markdown_text(value).translate(_TABLE_TRANSLATION)


def markdown_code_span(value: Any) -> str:
    """Render an untrusted value in an inline code span with a safe delimiter."""

    content = _single_line(value)
    if not content:
        return ""

    delimiter = "`" * (_longest_backtick_run(content) + 1)
    needs_padding = content.startswith("`") or content.endswith("`")
    if content.strip(" ") and (content.startswith(" ") or content.endswith(" ")):
        needs_padding = True
    padding = " " if needs_padding else ""
    return f"{delimiter}{padding}{content}{padding}{delimiter}"


def markdown_fenced_code(value: Any, *, language: str = "") -> str:
    """Render untrusted multiline code using a fence longer than its contents."""

    if not _SAFE_INFO_STRING.fullmatch(language):
        raise ValueError("Markdown fence language contains unsafe characters")

    content = _normalize_line_breaks(_string(value))
    fence = "`" * max(3, _longest_backtick_run(content) + 1)
    opening = f"{fence}{language}"
    separator = "" if content.endswith("\n") else "\n"
    return f"{opening}\n{content}{separator}{fence}"


def _single_line(value: Any) -> str:
    return _LINE_BREAKS.sub(" ", _string(value))


def _normalize_line_breaks(value: str) -> str:
    return _LINE_BREAKS.sub("\n", value)


def _longest_backtick_run(value: str) -> int:
    return max((len(match.group(0)) for match in _BACKTICK_RUNS.finditer(value)), default=0)


def _underscore_is_inside_identifier(value: str, index: int) -> bool:
    return (
        index > 0
        and index + 1 < len(value)
        and value[index - 1].isalnum()
        and value[index + 1].isalnum()
    )


def _string(value: Any) -> str:
    return "" if value is None else str(value)
