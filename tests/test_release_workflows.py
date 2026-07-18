from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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


def test_release_requires_guard_and_reusable_validation_before_publish() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert 'test "v$(uv version --short)" = "$GITHUB_REF_NAME"' in workflow
    assert 'git merge-base --is-ancestor "$tag_commit" refs/remotes/origin/main' in workflow
    assert "uses: ./.github/workflows/ci.yml" in workflow
    assert "upload_release_artifact: true" in workflow
    assert "needs: [guard, validate]" in workflow
    assert "environment: pypi" in workflow
    assert "id-token: write" in workflow
    assert "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093" in workflow
    assert "uv build" not in workflow


def test_release_accepts_manual_dispatch_so_tag_release_can_trigger_it() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    # the guard job's ref-based checks must still work unmodified for a
    # dispatch run made with --ref set to the tag (see tag-release.yml)
    assert 'test "v$(uv version --short)" = "$GITHUB_REF_NAME"' in workflow


def test_bump_version_workflow_validates_before_opening_a_release_pr() -> None:
    workflow = (ROOT / ".github" / "workflows" / "bump-version.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "options: [patch, minor, major]" in workflow
    assert "uses: ./.github/workflows/ci.yml" in workflow
    assert "needs: validate" in workflow
    assert "scripts/bump_version.py" in workflow
    assert "pull-requests: write" in workflow
    assert "gh pr create" in workflow
    assert "--base main" in workflow


def test_tag_release_workflow_is_idempotent_and_dispatches_release() -> None:
    workflow = (ROOT / ".github" / "workflows" / "tag-release.yml").read_text(encoding="utf-8")

    assert "paths:" in workflow
    assert "agentloop/version.py" in workflow
    assert 'git rev-parse "refs/tags/$TAG"' in workflow
    assert "exists=true" in workflow
    assert "if: steps.check.outputs.exists == 'false'" in workflow
    assert 'gh workflow run release.yml --ref "$TAG"' in workflow
    assert "actions: write" in workflow


def test_bump_version_script_exists_and_is_importable() -> None:
    script = (ROOT / "scripts" / "bump_version.py").read_text(encoding="utf-8")

    assert "def bump(" in script
    assert "class BumpError" in script
