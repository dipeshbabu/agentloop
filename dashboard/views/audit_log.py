"""The Audit Log page.

Split out of ``dashboard/app.py`` (issue #407).
"""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st


def render(ctx) -> None:
    store = ctx.store
    project_id = ctx.project_id
    limit_col, note_col = st.columns([1, 3], vertical_alignment="bottom")
    limit = limit_col.number_input(
        "Events to show", min_value=10, max_value=500, value=100, step=10
    )
    note_col.caption("Key values are never recorded.")
    events = store.list_audit_events(project_id, limit=int(limit))
    if not events:
        st.info("No audit events for this project yet.")
    else:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "When": event["occurred_at"],
                        "Action": event["action"],
                        "Actor": event["actor"],
                        "Target": event["target"],
                        "Details": json.dumps(event["details"], sort_keys=True),
                    }
                    for event in events
                ]
            ),
            width="stretch",
            hide_index=True,
        )
