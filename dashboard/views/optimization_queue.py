"""The Optimization Queue page.

Split out of ``dashboard/app.py`` (issue #407).
"""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from agentloop.issues import build_issue_drafts, issue_drafts_to_markdown
from agentloop.ranking import SORT_FIELDS, sort_ranked
from agentloop.store import (
    ALLOWED_FINDING_TRANSITIONS,
    FindingNotFoundError,
    FindingTransitionError,
)

try:
    from dashboard.common import (
        QUEUE_CACHE_SECONDS,
        SEVERITY_COLORS,
        _store_cache_token,
        cached_optimization_queue,
        invalidate_cached_results,
        money,
        seconds,
    )
except ModuleNotFoundError:
    from common import (
        QUEUE_CACHE_SECONDS,
        SEVERITY_COLORS,
        _store_cache_token,
        cached_optimization_queue,
        invalidate_cached_results,
        money,
        seconds,
    )


def render(ctx) -> None:
    store = ctx.store
    project_id = ctx.project_id
    dimension = st.selectbox(
        "Sort investigations by", SORT_FIELDS, format_func=lambda value: value.replace("_", " ")
    )
    cached_queue, computed_at = cached_optimization_queue(_store_cache_token(), project_id)
    queue = sort_ranked(cached_queue, dimension)
    finding_counts = store.finding_counts(project_id=project_id)
    refresh_note, refresh_button = st.columns([5, 1], vertical_alignment="center")
    refresh_note.caption(
        f"Queue computed at {computed_at}; it refreshes every {QUEUE_CACHE_SECONDS} seconds "
        "and after changes made here."
    )
    if refresh_button.button("Refresh", icon=":material/refresh:", width="stretch"):
        invalidate_cached_results()
        st.rerun()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Open opportunities", len(queue))
    c2.metric("Persisted findings", finding_counts["total"])
    c3.metric("Patchable findings", finding_counts["patchable"])
    c4.metric(
        "Estimated latency savings",
        seconds(
            None
            if any(item["estimated_latency_savings_ms"] is None for item in queue)
            else sum(item["estimated_latency_savings_ms"] for item in queue)
        ),
    )

    if not queue:
        st.info(
            "No optimization opportunities have been persisted yet. Store traces or run diagnosis first."
        )
    else:
        queue_df = pd.DataFrame(queue)
        queue_df["readiness"] = [item["ranking"]["status"].replace("_", " ") for item in queue]
        visible_cols = [
            "priority_score",
            "readiness",
            "severity",
            "type",
            "title",
            "occurrence_count",
            "run_count",
            "patchable_count",
            "quality_risk",
            "requires_scorer",
            "safe_to_auto_patch",
            "estimated_latency_savings_ms",
            "estimated_cost_savings_usd",
        ]
        st.dataframe(
            queue_df[visible_cols],
            width="stretch",
            hide_index=True,
            column_config={
                "priority_score": st.column_config.NumberColumn("Priority", width="small"),
                "readiness": st.column_config.TextColumn("Readiness"),
                "severity": st.column_config.TextColumn("Severity", width="small"),
                "type": st.column_config.TextColumn("Type"),
                "title": st.column_config.TextColumn("Finding", width="large"),
                "occurrence_count": st.column_config.NumberColumn("Occurrences"),
                "run_count": st.column_config.NumberColumn("Runs"),
                "patchable_count": st.column_config.NumberColumn("Patchable"),
                "quality_risk": st.column_config.TextColumn("Quality risk"),
                "requires_scorer": st.column_config.CheckboxColumn("Needs scorer"),
                "safe_to_auto_patch": st.column_config.CheckboxColumn("Auto-patch safe"),
                "estimated_latency_savings_ms": st.column_config.NumberColumn(
                    "Latency savings", format="%.0f ms"
                ),
                "estimated_cost_savings_usd": st.column_config.NumberColumn(
                    "Cost savings", format="$%.4f"
                ),
            },
        )

        st.divider()
        selected_title = st.selectbox("Open queue item", queue_df["title"].tolist())
        item = next(row for row in queue if row["title"] == selected_title)
        st.subheader(item["title"])
        readiness = item["ranking"]["status"].replace("_", " ")
        with st.container(horizontal=True, gap="small"):
            st.badge(
                f"{item['severity']} severity",
                color=SEVERITY_COLORS.get(item["severity"], "gray"),
            )
            st.badge(
                readiness,
                icon=":material/check_circle:" if readiness == "ready to test" else None,
                color="green" if readiness == "ready to test" else "orange",
            )
            st.badge(item["type"], color="gray")
        c5, c6, c7, c8 = st.columns(4)
        c5.metric("Investigation rank", item["ranking"]["priority_rank"])
        c6.metric("Affected runs", item["run_count"])
        c7.metric("Latency savings", seconds(item["estimated_latency_savings_ms"]))
        c8.metric("Cost savings", money(item["estimated_cost_savings_usd"]))
        st.dataframe(
            pd.DataFrame(
                [
                    ("Status", item["status"]),
                    ("Test readiness", readiness),
                    ("Quality risk", item["quality_risk"]),
                    ("Patchable occurrences", str(item["patchable_count"])),
                    ("Requires scorer", "yes" if item["requires_scorer"] else "no"),
                    ("Safe to auto-patch", "yes" if item["safe_to_auto_patch"] else "no"),
                ],
                columns=["Field", "Value"],
            ),
            hide_index=True,
            width="stretch",
        )
        with st.expander(f"Affected runs ({len(item['affected_runs'])})"):
            st.code("\n".join(item["affected_runs"]), language="text")
        with st.expander("Ranking inputs and provenance"):
            st.json(item["ranking"])

        item_findings = store.list_findings(
            project_id=project_id, type=item["type"], title=item["title"]
        )
        if item_findings:
            st.subheader("Finding instances")
            st.dataframe(pd.DataFrame(item_findings), width="stretch", hide_index=True)

            st.markdown("**Update finding status**")
            finding_labels = {
                f"{f['run_id']} · {f['finding_id']} (currently {f['status']})": f
                for f in item_findings
            }
            selected_finding_label = st.selectbox(
                "Finding", list(finding_labels.keys()), key=f"finding_select_{item['queue_id']}"
            )
            selected_finding = finding_labels[selected_finding_label]
            allowed_targets = sorted(
                ALLOWED_FINDING_TRANSITIONS.get(selected_finding["status"], ())
            )
            if allowed_targets:
                new_status = st.selectbox(
                    "New status", allowed_targets, key=f"status_select_{item['queue_id']}"
                )
                if st.button("Apply transition", key=f"apply_transition_{item['queue_id']}"):
                    try:
                        store.update_finding_status(
                            project_id,
                            selected_finding["run_id"],
                            selected_finding["finding_id"],
                            new_status,
                        )
                    except (FindingNotFoundError, FindingTransitionError) as exc:
                        st.error(str(exc))
                    else:
                        invalidate_cached_results()
                        st.success(f"{selected_finding['finding_id']} is now {new_status}")
                        st.rerun()
            else:
                st.caption(f"No transitions available from '{selected_finding['status']}'.")

        st.download_button(
            "Download optimization queue JSON",
            data=json.dumps({"project_id": project_id, "queue": queue}, indent=2),
            file_name="agentloop_optimization_queue.json",
            mime="application/json",
        )

        st.subheader("GitHub issue drafts")
        drafts = build_issue_drafts(queue)
        if not drafts:
            st.info("No patchable queue items available for issue drafts.")
        else:
            selected_issue = st.selectbox(
                "Preview issue draft", [draft["title"] for draft in drafts]
            )
            draft = next(item for item in drafts if item["title"] == selected_issue)
            st.write(f"Labels: `{', '.join(draft['labels'])}`")
            st.code(draft["body"], language="markdown")
            st.download_button(
                "Download issue drafts Markdown",
                data=issue_drafts_to_markdown(drafts),
                file_name="agentloop_issue_drafts.md",
                mime="text/markdown",
            )
