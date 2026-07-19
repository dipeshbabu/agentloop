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
    assert "default: patch" in workflow
    assert "patch (default) for routine changes" in workflow
    assert "uses: ./.github/workflows/ci.yml" in workflow
    assert "needs: validate" in workflow
    assert "scripts/bump_version.py" in workflow
    assert "pull-requests: write" in workflow
    assert "gh pr create" in workflow
    assert "--base main" in workflow


def test_releasing_docs_state_the_patch_default_versioning_policy() -> None:
    releasing = (ROOT / "docs" / "RELEASING.md").read_text(encoding="utf-8")

    assert "## Versioning policy" in releasing
    assert "Default to a **patch** bump" in releasing
    assert "Reserve\n**minor** for a release whose reason to ship is a notable" in releasing
    assert "Don't reach for `minor` just because a release happens to" in releasing


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


def test_bump_version_dispatches_the_branch_protection_required_checks() -> None:
    workflow = (ROOT / ".github" / "workflows" / "bump-version.yml").read_text(encoding="utf-8")

    assert "actions: write" in workflow
    assert 'gh workflow run "$workflow" --ref "$BRANCH"' in workflow
    assert "ci.yml codeql.yml dependency-review.yml agentloop-performance.yml" in workflow


def test_ci_and_codeql_accept_manual_dispatch_so_bump_version_can_trigger_them() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    codeql = (ROOT / ".github" / "workflows" / "codeql.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch: {}" in ci
    assert "workflow_dispatch: {}" in codeql
    # both jobs' names must still match the exact required-check context strings
    assert "name: Python ${{ matrix.python-version }}" in ci
    assert "name: Package artifact and wheel smoke" in ci
    assert "name: Docker image and deployment smoke" in ci
    assert "name: Analyze Python" in codeql


def test_manually_dispatched_ci_uploads_and_verifies_the_release_artifact() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    artifact_condition = (
        "inputs.upload_release_artifact || github.event_name == 'pull_request' "
        "|| github.event_name == 'workflow_dispatch'"
    )

    assert workflow.count(artifact_condition) == 3
    assert "Upload validated release artifact" in workflow
    assert "Download validated release artifact" in workflow
    assert "Verify downloaded artifact bytes" in workflow


def test_dependency_review_dispatch_path_uses_explicit_refs_without_touching_the_pr_path() -> None:
    workflow = (ROOT / ".github" / "workflows" / "dependency-review.yml").read_text(
        encoding="utf-8"
    )

    assert "workflow_dispatch: {}" in workflow
    assert "name: Review dependency changes" in workflow

    pr_step, dispatch_step = workflow.split("Review dependencies (pull request)")[1].split(
        "Review dependencies (manual dispatch against a branch)"
    )
    # the existing pull_request-triggered step must not gain base-ref/head-ref —
    # those aren't valid for a real pull_request event and would change behavior
    assert "base-ref" not in pr_step
    assert "head-ref" not in pr_step
    assert "fail-on-severity: moderate" in pr_step
    # the new dispatch-only step must set them explicitly, since the action
    # requires base-ref/head-ref outside a real pull_request event
    assert "base-ref: main" in dispatch_step
    assert "head-ref: ${{ github.ref_name }}" in dispatch_step
