import runpy
from pathlib import Path


def test_local_link_checks_ignore_installed_video_dependencies(tmp_path, monkeypatch):
    checker = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "check_repo.py"))
    monkeypatch.setitem(checker["markdown_files"].__globals__, "ROOT", tmp_path)
    (tmp_path / "README.md").write_text("[Guide](guide.md)", encoding="utf-8")
    (tmp_path / "guide.md").write_text("Local guide.", encoding="utf-8")
    dependency = tmp_path / "video" / "node_modules" / "example"
    dependency.mkdir(parents=True)
    (dependency / "README.md").write_text("[Unshipped source](missing.md)", encoding="utf-8")
    assert checker["check_markdown_links"]() == []
    (tmp_path / "guide.md").write_text("[Broken local link](missing.md)", encoding="utf-8")
    assert len(checker["check_markdown_links"]()) == 1
