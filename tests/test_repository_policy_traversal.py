"""Excluded dependency/build trees must not be traversed or hide eligible links."""

from pathlib import Path

from scripts import check_repo


def test_pruned_walk_preserves_existing_markdown_scope_and_link_failures(tmp_path, monkeypatch):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/target.txt").write_text("owned target", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "[good](docs/target.txt)\n[broken](docs/missing.md)\n", encoding="utf-8"
    )
    (tmp_path / "docs/notes.MD").write_text("owned mixed-case extension", encoding="utf-8")
    for name in check_repo.EXCLUDED_PARTS:
        folder = tmp_path / "docs" / name
        folder.mkdir()
        (folder / "ignored.md").write_text("[ignored](missing-target)\n", encoding="utf-8")
    expected = sorted(
        path
        for path in tmp_path.rglob("*.md")
        if not check_repo.EXCLUDED_PARTS.intersection(path.relative_to(tmp_path).parts)
    )
    walked = []
    real_walk = check_repo.os.walk

    def observe_walk(root):
        for directory, children, files in real_walk(root):
            walked.append(Path(directory))
            yield directory, children, files

    monkeypatch.setattr(check_repo, "ROOT", tmp_path)
    monkeypatch.setattr(check_repo.os, "walk", observe_walk)
    assert check_repo.markdown_files() == expected
    assert all(
        not check_repo.EXCLUDED_PARTS.intersection(path.relative_to(tmp_path).parts)
        for path in walked
    )
    errors = check_repo.check_markdown_links()
    assert len(errors) == 1
    assert "README.md" in errors[0] and "docs/missing.md" in errors[0]
