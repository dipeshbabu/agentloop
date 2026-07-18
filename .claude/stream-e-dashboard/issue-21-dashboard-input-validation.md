# Issue #21 — Handle invalid dashboard inputs without crashing the Streamlit app

- **Priority:** P2 · **Effort:** M · **Labels:** bug, code-quality
- **Link:** https://github.com/dipeshbabu/agentloop/issues/21

## Problem

Several dashboard controls parse or execute user input on **every rerun** with no validation
boundary. Invalid JSON, a malformed trace, or an out-of-root repo path raises straight from
page code — e.g. deleting one brace while editing the Quality Gates JSON reruns immediately
and throws `JSONDecodeError` before the edit is finished, replacing the whole page with an
exception.

## Key files

- `dashboard/app.py:386-388` — patch planning calls `build_patch_plan()` directly.
- `dashboard/app.py:537-539` — quality fixture upload/text decoded with `json.loads()`.
- `dashboard/app.py:654-656` — trace uploads decoded and converted directly.
- No form boundaries or inline exception handling around these paths.

## Approach

1. **Put expensive/validated actions behind `st.form` + submit buttons** (or explicit
   buttons) so they run on submit, not on every keystroke: patch planning, fixture parsing,
   trace upload/convert.
2. **Catch known parse/domain errors inline.** Wrap `json.loads`, trace conversion, and
   `build_patch_plan()` in try/except for the specific error types; show an inline, accessible
   message near the control describing the field and fix.
3. **Preserve input** across errors via session state so the user can correct and resubmit.
4. **Gate downstream actions:** only enable the store/plan action once input validates
   (e.g. invalid trace identifies schema/path error before the store button is active;
   out-of-root repo path shows the allowed-root requirement inline).
5. **Extract shared parse/validation helpers** (unit-testable) instead of inline logic.
6. Keep unexpected internal exceptions observable (logged) without leaking secrets/raw trace
   content to the user.

## Acceptance criteria (from the issue)

- [ ] Partial or malformed fixture JSON does not crash a rerun.
- [ ] Invalid trace uploads identify schema/path errors before the store action is enabled.
- [ ] Invalid patch repository paths show the allowed-root requirement inline.
- [ ] Errors state what to fix and remain adjacent to the relevant control.
- [ ] Previously entered text and selections survive validation errors.
- [ ] Success/error status updates are accessible to assistive technology via Streamlit-supported semantics.
- [ ] Shared parsing/validation helpers are unit tested; representative pages have Streamlit AppTest coverage.
- [ ] Unexpected internal exceptions remain observable without exposing secrets or raw trace content.

## Testing

- Unit-test the extracted helpers (valid, partial JSON, bad trace, out-of-root path).
- Streamlit **AppTest** on the Quality Gates, Ingest, and Patch Plan pages: malformed input
  does not raise; error shows; input persists.

## Compatibility / risk

- Moving actions behind forms changes interaction flow slightly (submit vs. live) — note in
  the changelog. No core/API/storage impact.
