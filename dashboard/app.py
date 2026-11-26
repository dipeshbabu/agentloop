from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import streamlit as st

try:
    from dashboard.access import (
        lockout_remaining,
        require_project_key,
        session_valid,
        sign_in,
        sign_out,
        visible_pages,
    )
    from dashboard.common import load_store, runs_directory
    from dashboard.theme import (
        PAGE_NAMES,
        apply_styles,
        nav_label,
        page_header,
    )
except ModuleNotFoundError:
    from access import (
        lockout_remaining,
        require_project_key,
        session_valid,
        sign_in,
        sign_out,
        visible_pages,
    )
    from common import load_store, runs_directory
    from theme import (
        PAGE_NAMES,
        apply_styles,
        nav_label,
        page_header,
    )

st.set_page_config(
    page_title="AgentLoop",
    page_icon=str(Path(__file__).with_name("assets") / "agentloop-mark.svg"),
    layout="wide",
)
apply_styles()


store = load_store()
# Read on every run, as before the split, so a changed setting takes effect.
runs_dir = runs_directory()
project_mode = require_project_key()

if project_mode:
    # A shared dashboard shows exactly one project: the one the API key belongs to.
    if "signed_in_project" in st.session_state and not session_valid(store, st.session_state):
        st.session_state["signin_notice"] = (
            "Your project API key was revoked or is no longer valid. Sign in again."
        )
    if "signed_in_project" not in st.session_state:
        _, center, _ = st.columns([1, 2, 1])
        with center:
            page_header(
                "Sign in",
                description="Enter a project API key. The dashboard shows only the traces "
                "and findings of the project that key belongs to.",
            )
            if notice := st.session_state.pop("signin_notice", None):
                st.warning(notice)
            with st.form("project_sign_in"):
                entered_key = st.text_input("Project API key", type="password")
                submitted = st.form_submit_button(
                    "Sign in", type="primary", icon=":material/login:", width="stretch"
                )
            if submitted:
                if sign_in(store, entered_key, st.session_state) is not None:
                    st.rerun()
                remaining = lockout_remaining(st.session_state)
                if remaining > 0:
                    st.error(
                        f"Too many failed attempts. Try again in {int(remaining) + 1} seconds."
                    )
                else:
                    st.error("That API key is not valid for any project.")
        st.stop()
    project_id = st.session_state["signed_in_project"]
    st.sidebar.markdown(f"**Project:** `{project_id}`")
    if st.sidebar.button("Sign out"):
        sign_out(st.session_state)
        st.rerun()
else:
    project_id = st.sidebar.text_input(
        "Project", value=st.session_state.get("project_id", "default")
    )
    st.session_state["project_id"] = project_id

# Recorded in the store's audit log for keys created and traces deleted here.
dashboard_actor = (
    f"dashboard:api-key:{st.session_state.get('signed_in_key_prefix', '')}"
    if project_mode
    else "dashboard"
)

page = st.sidebar.radio(
    "Navigate",
    visible_pages(PAGE_NAMES, project_mode=project_mode),
    format_func=nav_label,
    label_visibility="collapsed",
)

page_header(page, project_id)


# Each page lives in dashboard/views (#407).
try:
    from dashboard.views import (
        api_keys,
        audit_log,
        ci_runs,
        diagnosis,
        ingest,
        optimization,
        optimization_queue,
        overview,
        patch_plan,
        quality_gates,
        replay_proof,
        setup,
        traces,
        value_pricing,
    )
except ModuleNotFoundError:
    from views import (
        api_keys,
        audit_log,
        ci_runs,
        diagnosis,
        ingest,
        optimization,
        optimization_queue,
        overview,
        patch_plan,
        quality_gates,
        replay_proof,
        setup,
        traces,
        value_pricing,
    )

VIEWS = {
    "Overview": overview,
    "Traces": traces,
    "Optimization Queue": optimization_queue,
    "Optimization": optimization,
    "Diagnosis": diagnosis,
    "Patch Plan": patch_plan,
    "CI Runs": ci_runs,
    "Replay Proof": replay_proof,
    "Quality Gates": quality_gates,
    "Value & Pricing": value_pricing,
    "Audit Log": audit_log,
    "API Keys": api_keys,
    "Ingest": ingest,
    "Setup": setup,
}
VIEWS[page].render(
    SimpleNamespace(
        store=store,
        project_id=project_id,
        project_mode=project_mode,
        dashboard_actor=dashboard_actor,
        runs_dir=runs_dir,
    )
)
