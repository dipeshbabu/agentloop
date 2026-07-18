# Stream E — Dashboard robustness

## Scope

The Streamlit operator dashboard only: **`dashboard/app.py`** (and `dashboard/value_view.py`),
plus small shared parse/validation helpers that can live in the dashboard package or a
dashboard-facing util. Does **not** touch core analysis or storage.

Issue: **#21** (handle invalid dashboard inputs without crashing).

## Approach for the stream as a whole

One issue, **M** effort. The theme: Streamlit reruns on every keystroke, so any parse/execute
call sitting directly in page code turns a half-typed edit into a full-page exception. The fix
is to (a) move expensive/validated actions behind explicit forms/buttons, and (b) translate
known parse/domain errors into inline, accessible feedback while preserving the user's input.

## Stream-specific rules

- Keep the dashboard **inside its boundary** — it consumes core/store APIs; it must not
  reimplement validation that belongs in core. Prefer calling existing validators and
  catching their typed errors.
- Preserve entered text/selections across a validation error (use session state / form
  semantics) — the user must be able to fix and resubmit.
- Error messages must say *what to fix* and stay adjacent to the control; status updates must
  be accessible to assistive tech via Streamlit-supported semantics.
- Never surface secrets or raw trace content in an error view.
- Cover shared helpers with unit tests and representative pages with Streamlit **AppTest**.

## Cross-stream coordination

- **#19 (Stream A)** changes how recent-runs/findings lists are loaded and rendered here
  (pagination). If both are active, align on the list-rendering code path.
- If **#13 (Stream C)** lands a shared validation/error contract, reuse it for trace-upload
  validation rather than duplicating.

## Definition of done for the stream

#21's acceptance criteria met; partial/malformed input no longer crashes a rerun; AppTest
coverage on representative pages; changelog updated.

See [`../SHARED_CONVENTIONS.md`](../SHARED_CONVENTIONS.md).
