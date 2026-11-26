"""The Setup page.

Split out of ``dashboard/app.py`` (issue #407).
"""

from __future__ import annotations

import streamlit as st


def render(ctx) -> None:
    project_id = ctx.project_id
    st.subheader("Local development")
    st.code(
        "uv sync --locked --all-extras --dev\n"
        "uv run agentloop init-store\n"
        "uv run agentloop demo-all\n"
        f"uv run agentloop store-trace --path runs/research_agent_baseline.json --project-id {project_id}\n"
        "uv run streamlit run dashboard/app.py",
        language="bash",
    )

    st.subheader("Hosted API")
    st.code(
        "uv run agentloop create-api-key --project-id acme --name local-dev\n"
        "uv run agentloop server --host 127.0.0.1 --port 8000\n"
        "uv run agentloop upload --path runs/research_agent_baseline.json "
        "--api-url http://127.0.0.1:8000 --api-key al_xxx\n"
        "uv run agentloop remote-usage --api-url http://127.0.0.1:8000 --api-key al_xxx",
        language="bash",
    )

    st.subheader("Postgres deployment")
    st.code(
        "uv sync --locked --extra server --extra postgres --no-dev\n"
        "export AGENTLOOP_STORE_BACKEND=postgres\n"
        "export AGENTLOOP_DATABASE_URL=postgresql://agentloop:agentloop@localhost:5432/agentloop\n"
        "uv run agentloop init-store\n"
        "uv run agentloop server --host 0.0.0.0 --port 8000",
        language="bash",
    )
