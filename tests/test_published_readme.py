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
