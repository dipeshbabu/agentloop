# Build the documentation

The documentation site uses Material for MkDocs with the existing Markdown guides
as its source. Python, TypeScript, contribution, release and security content
keeps its canonical repository location.

The tools are pinned to MkDocs 1.6.1 and Material for MkDocs 9.8.1. Upstream
provides critical fixes and security updates through May 5, 2027.
[Upstream maintenance schedule](https://squidfunk.github.io/mkdocs-material/changelog/)

## Local preview

From a source checkout, install the documentation tools and start the preview.

```bash
uv sync --locked --dev --group docs
uv run --no-sync mkdocs serve
```

Open the URL printed by MkDocs. The preview uses the `/agentloop/` project path
from the public site configuration.

Run the strict build and documentation regression tests before opening a PR.

```bash
uv run --no-sync python -m pytest tests/test_docs_site.py -q
uv run --no-sync mkdocs build --strict
```

The generated site is written to `runs/docs-site`. Edit navigation in
`mkdocs.yml` and the theme in `docs/stylesheets/agentloop.css`.

## Sources and links

Most pages come directly from `docs/`. The build includes the canonical
TypeScript README, contribution guide, changelog and security policy as generated
pages. Edit those original files to update their published content.

The documentation hook keeps links between guides inside the site. Links to
repository examples, source files, licenses and research archives open their
GitHub locations. Code examples retain their original text.

## GitHub Pages

The Documentation workflow builds and checks pull requests. Changes merged into
`main` deploy through the `github-pages` environment using GitHub's Pages actions.
The workflow uses the repository's normal checks and review rules.

The published site is [AgentLoop documentation](https://dipeshbabu.github.io/agentloop/).
