# Issue #27 — Harden public repository settings and align tracker labels

- **Priority:** P0/P1 · **Effort:** S · **Labels:** documentation, enhancement, github-actions
- **Link:** https://github.com/dipeshbabu/agentloop/issues/27
- Mostly **GitHub settings** (repo-admin) plus small YAML edits.

## Problem

Source-controlled security workflows pass, but the surrounding repo controls don't enforce
them. As of the audit: `main` has no branch protection; private vulnerability reporting is
off (though `SECURITY.md` links to it); Dependabot security updates are off; Actions are
allowed from any source; and issue forms/optimization drafts reference labels that don't exist
(`triage`, `agentloop`, `agent-performance`, dynamic `agentloop:<type>`).

## Key files / settings

- Issue-form YAML in `.github/ISSUE_TEMPLATE/` — request the nonexistent `triage` label.
- Generated optimization issue drafts (`agentloop/issues.py`) — request nonexistent labels.
- GitHub repository settings — branch/tag protection, security features, Actions policy.
- `docs/OPEN_SOURCE_CHECKLIST.md` — the operational checklist to complete/record.

## Approach

**Owner/admin settings (cannot be automated — flag as owner actions):**
- Add a branch ruleset protecting `main`: PR required, selected required checks, review
  conversations resolved, force-push/delete blocked, narrow bypass.
- Protect release tags matching `v*`.
- Enable **private vulnerability reporting**; verify the `SECURITY.md` link as a non-maintainer.
- Enable dependency graph, Dependabot alerts, and **Dependabot security updates**.
- Set Actions policy to allow only required sources / enforce SHA-pinning.

**Agent-preparable PR:**
1. Fix label references: create `triage` **or** remove it from all forms; make generated issue
   drafts (`issues.py`) use existing stable labels (no unbounded dynamic `agentloop:<type>`
   taxonomy) — or create/document the required labels deliberately.
2. Record the final settings in `docs/OPEN_SOURCE_CHECKLIST.md` and note the periodic review.
3. Choose required checks so external contributors still get actionable PR results without
   unsafe write permissions.

## Acceptance criteria (from the issue)

- [ ] A branch ruleset protects `main` (PRs, required checks, resolved conversations, no force-push/delete, narrow bypass). *(owner)*
- [ ] Release tags matching the documented pattern are protected. *(owner)*
- [ ] Private vulnerability reporting enabled and the `SECURITY.md` link verified. *(owner)*
- [ ] Dependabot security updates and dependency graph/alerts enabled. *(owner)*
- [ ] Actions policy allows only required sources or enforces pinning. *(owner)*
- [ ] `triage` is created or removed from all forms.
- [ ] Generated issue drafts use existing stable labels, or required labels are created/documented without unbounded dynamic taxonomy.
- [ ] The launch checklist records the final settings and an owner reviews them periodically.
- [ ] Required checks let external contributors get actionable PR results without unsafe write permissions.

## Testing

- Validate issue-form/`issues.py` YAML/label output against the actual label set (e.g. a small
  test asserting drafts only use known labels).
- Manually confirm settings post-change (owner) and re-verify the `SECURITY.md` link.

## Compatibility / risk

- Label changes affect issue automation — make the code and the actual GitHub label set agree
  in the same change.
- The branch-protection here is a precondition for #25's "reachable from protected default
  branch" gate.
