"""Publish canonical guides and route repository links for the documentation site."""

from __future__ import annotations

import os
import re
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


def on_page_markdown(markdown, page, config, files):
    source = SOURCE_PAGES.get(page.file.src_uri, DOCS / page.file.src_uri)
    return rewrite_links(markdown, source, page.file.src_uri)


def on_config(config):
    from pymdownx.superfences import fence_code_format

    config["mdx_configs"]["pymdownx.superfences"] = {
        "custom_fences": [{"name": "mermaid", "class": "mermaid", "format": fence_code_format}]
    }
    return config
