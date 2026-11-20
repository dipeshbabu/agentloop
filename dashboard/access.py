"""Project-scoped sign-in for a shared, hosted dashboard.

With ``AGENTLOOP_DASHBOARD_REQUIRE_KEY`` enabled, the dashboard asks for a
project API key, verifies it against the store exactly as the HTTP API does,
and shows only that key's project. Pages that act outside one project are
hidden. Without the setting, the dashboard keeps its local, single-user
behaviour with a free-text project field.
"""

from __future__ import annotations

import os
import time

# Creating keys is an administrator task, and the patch plan reads files on the
# dashboard host; neither belongs to one project's users.
PROJECT_HIDDEN_PAGES = frozenset({"API Keys", "Patch Plan"})
MAX_FAILURES = 5
LOCKOUT_SECONDS = 60.0


def require_project_key() -> bool:
    """Return whether the dashboard must sign users in with a project API key."""
    return os.getenv("AGENTLOOP_DASHBOARD_REQUIRE_KEY", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def visible_pages(pages: list[str], *, project_mode: bool) -> list[str]:
    """Pages a signed-in project user may open."""
    if not project_mode:
        return list(pages)
    return [page for page in pages if page not in PROJECT_HIDDEN_PAGES]


def lockout_remaining(state, now: float | None = None) -> float:
    """Seconds left before another sign-in attempt is allowed."""
    now = time.monotonic() if now is None else now
    return max(0.0, float(state.get("signin_locked_until", 0.0)) - now)


def sign_in(store, api_key: str, state, now: float | None = None) -> str | None:
    """Verify a project API key and record the signed-in project in ``state``.

    Returns the project ID on success. Failed attempts are counted per session,
    and after ``MAX_FAILURES`` further attempts wait ``LOCKOUT_SECONDS``.
    """
    now = time.monotonic() if now is None else now
    if lockout_remaining(state, now) > 0:
        return None
    key = api_key.strip() if isinstance(api_key, str) else ""
    record = store.verify_api_key(key) if key else None
    if record is None:
        failures = int(state.get("signin_failures", 0)) + 1
        state["signin_failures"] = failures
        if failures >= MAX_FAILURES:
            state["signin_failures"] = 0
            state["signin_locked_until"] = now + LOCKOUT_SECONDS
        return None
    state["signin_failures"] = 0
    state["signed_in_project"] = str(record["project_id"])
    state["signed_in_key_prefix"] = str(record.get("prefix") or "")
    return state["signed_in_project"]


def sign_out(state) -> None:
    for name in ("signed_in_project", "signed_in_key_prefix"):
        state.pop(name, None)
