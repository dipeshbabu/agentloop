from __future__ import annotations

import pytest

from scripts import check_repo


def test_repository_readme_can_be_rendered_on_pypi():
    assert check_repo.check_readme_links() == []


@pytest.mark.parametrize("target", ["docs/GUIDED_OPTIMIZATION.md", "SUPPORT.md", "/docs/HERMES.md"])
def test_published_readme_check_rejects_repository_relative_links(tmp_path, monkeypatch, target):
    (tmp_path / "README.md").write_text(f"[Guide]({target})", encoding="utf-8")
    monkeypatch.setattr(check_repo, "ROOT", tmp_path)
    assert len(check_repo.check_readme_links()) == 1


@pytest.mark.parametrize(
    "target",
    [
        "#guided-optimization",
        "https://example.test/guide/",
        "<https://example.test/guide/>",
        "mailto:maintainer@example.test",
    ],
)
def test_published_readme_check_keeps_fragments_and_absolute_urls(tmp_path, monkeypatch, target):
    (tmp_path / "README.md").write_text(f"[Guide]({target})", encoding="utf-8")
    monkeypatch.setattr(check_repo, "ROOT", tmp_path)
    assert check_repo.check_readme_links() == []


@pytest.mark.parametrize(
    "markup",
    [
        '<img src="docs/logo.svg">',
        '<a href="SUPPORT.md">Support</a>',
        '<source srcset="docs/mobile.svg 600w, https://example.test/full.gif 1200w">',
    ],
)
def test_published_readme_rejects_relative_html_destinations(tmp_path, monkeypatch, markup):
    (tmp_path / "README.md").write_text(markup, encoding="utf-8")
    monkeypatch.setattr(check_repo, "ROOT", tmp_path)
    assert len(check_repo.check_readme_links()) == 1


def test_published_readme_keeps_absolute_html_and_data_images(tmp_path, monkeypatch):
    markup = '<source srcset="https://example.test/mobile.svg 600w, https://example.test/full.gif 1200w"><img src="data:image/png;base64,AAAA">'
    (tmp_path / "README.md").write_text(markup, encoding="utf-8")
    monkeypatch.setattr(check_repo, "ROOT", tmp_path)
    assert check_repo.check_readme_links() == []
