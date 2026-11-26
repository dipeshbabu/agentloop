"""The API Keys page.

Split out of ``dashboard/app.py`` (issue #407).
"""

from __future__ import annotations

import streamlit as st


def render(ctx) -> None:
    store = ctx.store
    project_id = ctx.project_id
    dashboard_actor = ctx.dashboard_actor
    with st.container(border=True):
        st.markdown("#### Create a project API key")
        st.caption(
            f"The key can upload and read traces in project `{project_id}`. It is shown "
            "only once, so copy it before leaving this page."
        )
        name_col, button_col = st.columns([3, 1], vertical_alignment="bottom")
        key_name = name_col.text_input("Key name", value="local-dev")
        create = button_col.button(
            "Create API key", type="primary", icon=":material/key:", width="stretch"
        )
        if create:
            created = store.create_api_key(
                project_id=project_id, name=key_name, actor=dashboard_actor
            )
            st.success(f"Created key for project `{project_id}`", icon=":material/check_circle:")
            st.code(created["api_key"])

    st.subheader("Use this key")
    st.code(
        "uv run agentloop upload --path runs/research_agent_baseline.json "
        "--api-url http://127.0.0.1:8000 --api-key YOUR_KEY",
        language="bash",
    )
