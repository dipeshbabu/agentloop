from __future__ import annotations

from pathlib import Path

from agentloop import patches


def test_patch_index_prunes_generated_trees_and_keeps_application_sources(tmp_path, monkeypatch):
    (tmp_path / "agent.py").write_text("def answer(): return 1\n", encoding="utf-8")
    for name in ("node_modules", "build", "dist", ".package-smoke", ".hermes-runtime"):
        folder = tmp_path / name / "vendor"
        folder.mkdir(parents=True)
        (folder / "unsafe.py").write_text("from langgraph import StateGraph\n", encoding="utf-8")
    visited = []
    original = patches.os.walk

    def walk(*args, **kwargs):
        for directory, names, files in original(*args, **kwargs):
            visited.append(Path(directory))
            yield directory, names, files

    monkeypatch.setattr(patches.os, "walk", walk)
    index = patches._index_source_files(tmp_path)
    assert [item["path"] for item in index] == ["agent.py"]
    assert visited == [tmp_path]
    assert patches._detect_frameworks(index) == ["plain_python"]
