from pathlib import Path

from scripts import docs_hooks


def test_repository_links_resolve_from_the_original_document():
    result = docs_hooks.rewrite_links(
        "[Runner](../examples/hermes_repository_runner.py#main)\n",
        docs_hooks.DOCS / "GUIDED_OPTIMIZATION.md",
        "GUIDED_OPTIMIZATION.md",
    )
    assert (
        result
        == "[Runner](https://github.com/dipeshbabu/agentloop/blob/main/examples/hermes_repository_runner.py#main)\n"
    )


def test_generated_pages_keep_links_to_guides_inside_the_site():
    assert (
        docs_hooks.site_link(
            "docs/RELEASING.md", docs_hooks.ROOT / "CONTRIBUTING.md", "development/contributing.md"
        )
        == "../RELEASING.md"
    )
    assert (
        docs_hooks.site_link(
            "../../docs/TRACE_SCHEMA.md#token-provenance",
            docs_hooks.ROOT / "sdk/typescript/README.md",
            "reference/typescript.md",
        )
        == "../TRACE_SCHEMA.md#token-provenance"
    )


def test_existing_guide_links_and_external_urls_stay_valid():
    source = docs_hooks.DOCS / "FIRST_USE.md"
    for link in ("#example", "TRACE_SCHEMA.md", "https://example.test/docs?a=b#usage"):
        assert docs_hooks.site_link(link, source, "FIRST_USE.md") == link


def test_code_examples_are_preserved_while_document_links_are_rewritten():
    text = "```markdown\n[Source](../README.md)\n```\n[Source](../README.md)\n"
    result = docs_hooks.rewrite_links(text, docs_hooks.DOCS / "FIRST_USE.md", "FIRST_USE.md")
    assert result.startswith("```markdown\n[Source](../README.md)\n```\n")
    assert result.endswith(
        "[Source](https://github.com/dipeshbabu/agentloop/blob/main/README.md)\n"
    )


def test_generated_content_reads_the_canonical_files():
    import pytest

    pytest.importorskip("mkdocs")
    from mkdocs.config import load_config
    from mkdocs.structure.files import Files

    config = load_config(str(docs_hooks.ROOT / "mkdocs.yml"))
    files = config.plugins.run_event("files", Files([]), config=config)
    for uri, source in docs_hooks.SOURCE_PAGES.items():
        assert files.get_file_from_path(uri).content_string == source.read_text(encoding="utf-8")
    assert Path(config.docs_dir) == docs_hooks.DOCS
