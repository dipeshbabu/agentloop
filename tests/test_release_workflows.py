from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOWNLOAD_ARTIFACT_SHA = "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"


def test_ci_exposes_reusable_full_validation_and_release_artifact() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "workflow_call:" in workflow
    assert 'python-version: ["3.10", "3.13"]' in workflow
    assert "uv run --frozen pre-commit run --all-files" in workflow
    assert "uv run --frozen --all-extras python -m pytest -q" in workflow
    assert "Build distribution once" in workflow
    assert "twine check dist/*" in workflow
    assert ".package-smoke/bin/agentloop --help" in workflow
    assert "Docker image and deployment smoke" in workflow
    assert "upload_release_artifact" in workflow
    assert "name: python-package" in workflow
    assert f"actions/download-artifact@{DOWNLOAD_ARTIFACT_SHA}" in workflow
    assert "Download validated release artifact" in workflow
    assert ".artifact-download-smoke" in workflow
    assert "sha256sum * | sort" in workflow


def test_release_requires_guard_and_reusable_validation_before_publish() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert 'test "v$(uv version --short)" = "$GITHUB_REF_NAME"' in workflow
    assert 'git merge-base --is-ancestor "$tag_commit" refs/remotes/origin/main' in workflow
    assert "uses: ./.github/workflows/ci.yml" in workflow
    assert "upload_release_artifact: true" in workflow
    assert "needs: [guard, validate]" in workflow
    assert "environment: pypi" in workflow
    assert "id-token: write" in workflow
    assert f"actions/download-artifact@{DOWNLOAD_ARTIFACT_SHA}" in workflow
    assert "uv build" not in workflow
