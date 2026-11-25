"""Publish canonical guides and route repository links for the documentation site."""

from __future__ import annotations

import os
import re
from html import escape, unescape
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SOURCE_PAGES = {
    "reference/typescript.md": ROOT / "sdk/typescript/README.md",
    "development/contributing.md": ROOT / "CONTRIBUTING.md",
    "development/changelog.md": ROOT / "CHANGELOG.md",
    "development/security.md": ROOT / "SECURITY.md",
}
LINK = re.compile(r"(!?\[[^\]\n]*\]\()(<[^>\n]+>|[^\s)]+)([^)\n]*\))")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


def on_files(files, config):
    from mkdocs.structure.files import File

    for uri, source in SOURCE_PAGES.items():
        files.append(File.generated(config, uri, content=source.read_text(encoding="utf-8")))
    return files


def site_link(target: str, source: Path, destination: str) -> str:
    """Keep documentation links local and route other public files to GitHub."""
    angled = target.startswith("<") and target.endswith(">")
    parsed = urlsplit(target[1:-1] if angled else target)
    if parsed.scheme or parsed.netloc or not parsed.path or parsed.path.startswith("/"):
        return target
    resolved = (source.parent / unquote(parsed.path)).resolve()
    if not resolved.is_relative_to(ROOT) or not resolved.exists():
        return target
    generated = {path.resolve(): uri for uri, path in SOURCE_PAGES.items()}
    if resolved.is_relative_to(DOCS):
        uri = resolved.relative_to(DOCS).as_posix()
    elif resolved in generated:
        uri = generated[resolved]
    else:
        kind = "tree" if resolved.is_dir() else "blob"
        uri = f"https://github.com/dipeshbabu/agentloop/{kind}/main/" + quote(
            resolved.relative_to(ROOT).as_posix(), safe="/"
        )
        return urlunsplit((*urlsplit(uri)[:3], parsed.query, parsed.fragment))
    relative = Path(os.path.relpath(uri, Path(destination).parent)).as_posix()
    rewritten = urlunsplit(("", "", relative, parsed.query, parsed.fragment))
    return f"<{rewritten}>" if angled else rewritten


def rewrite_links(markdown: str, source: Path, destination: str) -> str:
    lines = []
    fence = None
    for line in markdown.splitlines(keepends=True):
        marker = FENCE.match(line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence) and line.strip() == token:
                fence = None
            lines.append(line)
        elif fence is not None:
            lines.append(line)
        else:
            lines.append(
                LINK.sub(
                    lambda match: match[1] + site_link(match[2], source, destination) + match[3],
                    line,
                )
            )
    return "".join(lines)


PAGE_ICONS = {
    "index.md": "octicons/home-16",
    "installation.md": "octicons/download-16",
    "FIRST_USE.md": "octicons/zap-16",
    "ONBOARDING.md": "octicons/plug-16",
    "GUIDED_OPTIMIZATION.md": "octicons/sparkles-fill-16",
    "DOCTOR.md": "octicons/pulse-16",
    "FINDING_RULES.md": "octicons/search-16",
    "FINDING_RANKING.md": "octicons/list-ordered-16",
    "STRUCTURED_QUALITY.md": "octicons/verified-16",
    "CI.md": "octicons/git-pull-request-16",
    "HTML_REPORTS.md": "octicons/graph-16",
    "PRICING.md": "octicons/number-16",
    "TRACE_RETENTION.md": "octicons/archive-16",
    "STUDIES.md": "octicons/beaker-16",
    "INTERVENTIONS.md": "octicons/tasklist-16",
    "INTEGRATIONS.md": "octicons/apps-16",
    "HERMES.md": "octicons/hubot-16",
    "CODEX.md": "octicons/terminal-16",
    "CLAUDE_CODE.md": "octicons/terminal-16",
    "LANGGRAPH_HARNESS.md": "octicons/workflow-16",
    "HARBOR_TRIALS.md": "octicons/milestone-16",
    "HARBOR_ATIF.md": "octicons/file-binary-16",
    "CROSS_HARNESS_STUDIES.md": "octicons/git-compare-16",
    "OMNIGENT_OTEL.md": "octicons/broadcast-16",
    "OTLP_JSONL.md": "octicons/file-code-16",
    "INTEGRATION_CONFORMANCE.md": "octicons/shield-check-16",
    "python-sdk.md": "octicons/code-16",
    "reference/typescript.md": "octicons/file-code-16",
    "API_VERSIONING.md": "octicons/server-16",
    "TRACE_SCHEMA.md": "octicons/file-16",
    "SOURCE_PROVENANCE.md": "octicons/code-square-16",
    "EXECUTION_IR.md": "octicons/workflow-16",
    "TIMING_SENSITIVITY.md": "octicons/stopwatch-16",
    "TRANSFORMATIONS.md": "octicons/diff-16",
    "REQUEST_REUSE.md": "octicons/cache-16",
    "SCHEDULE_COMPILER.md": "octicons/git-merge-queue-16",
    "RETRY_PASSES.md": "octicons/sync-16",
    "OPTIMIZATION_SEARCH.md": "octicons/goal-16",
    "WORKFLOW_TRACING.md": "octicons/git-branch-16",
    "DASHBOARD.md": "octicons/project-roadmap-16",
    "PRODUCTION.md": "octicons/stack-16",
    "CLOUD_DEPLOY.md": "octicons/cloud-16",
    "KUBERNETES.md": "octicons/container-16",
    "OPERATIONS.md": "octicons/pulse-16",
    "HARNESS.md": "octicons/shield-16",
    "HARNESS_CAPABILITIES.md": "octicons/key-16",
    "BUDGETS.md": "octicons/meter-16",
    "COMPLETION.md": "octicons/check-circle-16",
    "CHECKPOINT_RECOVERY.md": "octicons/history-16",
    "CONTEXT_CONTROLS.md": "octicons/stack-16",
    "TOOL_SCHEDULING.md": "octicons/clock-16",
    "MODEL_ROUTING.md": "octicons/repo-forked-16",
    "GUIDED_VALIDATION.md": "octicons/checklist-16",
    "HERMES_OPTIMIZATION_CASE.md": "octicons/graph-16",
    "GUIDED_LOCAL_MODEL_CASE.md": "octicons/x-circle-16",
    "HERMES_RUNTIME_PILOT.md": "octicons/cpu-16",
    "ESTIMATORS.md": "octicons/number-16",
    "CALIBRATION.md": "octicons/sliders-16",
    "REAL_AGENT_STUDY.md": "octicons/telescope-16",
    "RESEARCH.md": "octicons/book-16",
    "ADOPTION_GAPS.md": "octicons/circle-16",
    "development/contributing.md": "octicons/git-merge-16",
    "documentation.md": "octicons/book-16",
    "development/changelog.md": "octicons/log-16",
    "development/security.md": "octicons/lock-16",
    "ROADMAP.md": "octicons/milestone-16",
    "UPGRADING.md": "octicons/arrow-up-16",
    "RELEASING.md": "octicons/rocket-16",
}


def on_page_markdown(markdown, page, config, files):
    # Sidebar icons come from here so the Markdown files stay plain for GitHub.
    icon = PAGE_ICONS.get(page.file.src_uri)
    if icon and "icon" not in page.meta:
        page.meta["icon"] = icon
    source = SOURCE_PAGES.get(page.file.src_uri, DOCS / page.file.src_uri)
    return rewrite_links(markdown, source, page.file.src_uri)


def section_label(page) -> str | None:
    """The navigation section a page sits in, shown above its title."""
    parent = getattr(page, "parent", None)
    title = getattr(parent, "title", None)
    return str(title) if title else None


FIRST_PARAGRAPH = re.compile(r"<p\b[^>]*>(.*?)</p>", re.DOTALL)
TAG = re.compile(r"<[^>]+>")
SUMMARY_LENGTH = 90


def page_summary(html: str, description: str | None = None) -> str | None:
    """One line describing a page: its description, else its first paragraph.

    Plain text, cut at a word boundary; templates escape it when rendering.
    """
    text = description
    if not text:
        heading = html.find("</h1>")
        match = FIRST_PARAGRAPH.search(html, heading if heading >= 0 else 0)
        text = unescape(TAG.sub("", match.group(1))) if match else None
    if not text:
        return None
    text = " ".join(text.split())
    if len(text) <= SUMMARY_LENGTH:
        return text
    cut = text[: SUMMARY_LENGTH + 1].rsplit(" ", 1)[0].rstrip(" ,;:.")
    return cut + "…"


def on_page_content(html, page, config, files):
    # Read by overrides/main.html for the "Next" card. Every page's content is
    # built before any template renders, so neighbours' summaries are ready.
    summary = page_summary(html, page.meta.get("description"))
    if summary:
        page.meta["al_summary"] = summary
    label = section_label(page)
    heading = html.find("<h1")
    if label is None or heading < 0:
        return html
    eyebrow = f'<p class="al-eyebrow">{escape(label)}</p>'
    return html[:heading] + eyebrow + html[heading:]


def on_config(config):
    from pymdownx.superfences import fence_code_format

    config["mdx_configs"]["pymdownx.superfences"] = {
        "custom_fences": [{"name": "mermaid", "class": "mermaid", "format": fence_code_format}]
    }
    return config
