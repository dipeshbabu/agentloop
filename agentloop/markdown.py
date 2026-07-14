"""Context-aware Markdown escaping for exporter output.

Exporters interpolate trace- and finding-derived strings (names, event names,
optimization titles, descriptions, evidence, ...) into Markdown. Those values
can originate in model/tool metadata or imported telemetry, so they may contain
characters that are structurally meaningful in Markdown. Interpolating them raw
lets a value inject headings, break tables, terminate code spans, or smuggle raw
HTML.

These helpers escape for a *destination context* rather than applying one global
replacement. Callers pick the helper that matches where the value lands:

- :func:`escape_inline` — prose, headings, list items, bold/italic content, and
  table cells (inline contexts on a single line).
- :func:`escape_cell` — alias of :func:`escape_inline`, named for table cells so
  call sites read clearly.
- :func:`code_span` — a value rendered inside inline code; picks a backtick fence
  that the content can't break out of.

Emitting Markdown links is intentionally out of scope: no exporter emits
trace-derived link labels or destinations today. If one starts to, add a
dedicated helper that encodes the label and destination in their own contexts.
"""

from __future__ import annotations

import re

# CommonMark backslash-escapable ASCII punctuation that can start or alter a
# block or inline construct when it appears in interpolated text. Backslash is
# included and, because we escape in a single ``str.translate`` pass, it is not
# double-escaped.
_ESCAPABLE = "\\`*_{}[]()#+-.!|~"
_ESCAPE_TABLE = {ord(ch): f"\\{ch}" for ch in _ESCAPABLE}

_WHITESPACE_RUN = re.compile(r"\s+")
_BACKTICK_RUN = re.compile(r"`+")


def escape_inline(value: object) -> str:
    """Escape ``value`` for an inline, single-line Markdown context.

    Safe for headings, list-item text, bold/italic content, and table cells:

    - Raw HTML is neutralized by entity-encoding ``&``, ``<`` and ``>``.
    - Markdown-significant punctuation is backslash-escaped, so the text cannot
      open a heading/list/emphasis/link/code construct. ``|`` is escaped too, so
      a table cell keeps its column count.
    - All whitespace runs (including newlines) collapse to single spaces, so the
      value cannot introduce a new line, heading, or block.
    """
    text = str(value)
    # Entity-encode HTML-significant characters first; the resulting ``&`` in an
    # entity is not in the escapable set, so the translate pass leaves it alone.
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = text.translate(_ESCAPE_TABLE)
    return _WHITESPACE_RUN.sub(" ", text).strip()


def escape_cell(value: object) -> str:
    """Escape ``value`` for a Markdown table cell. Alias of :func:`escape_inline`
    (which already escapes ``|`` and collapses newlines), named for readability
    at table call sites."""
    return escape_inline(value)


def code_span(value: object) -> str:
    """Render ``value`` as an inline code span that the content cannot escape.

    Picks a backtick fence one longer than the longest backtick run inside the
    text (CommonMark rule) and pads with spaces when the content starts or ends
    with a backtick or is only backticks. Newlines collapse to spaces, since a
    code span is single-line. Returns the complete span including the fences.
    """
    text = _WHITESPACE_RUN.sub(" ", str(value))
    longest = max((len(run) for run in _BACKTICK_RUN.findall(text)), default=0)
    fence = "`" * (longest + 1)
    if text.startswith("`") or text.endswith("`") or (text and text.strip("`") == ""):
        return f"{fence} {text} {fence}"
    return f"{fence}{text}{fence}"
