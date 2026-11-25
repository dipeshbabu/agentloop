"""Shared look for the dashboard: page metadata, navigation labels and styles.

Colours come from .streamlit/config.toml. The CSS here only shapes components
the theme cannot reach (the sidebar navigation, metric cards and the page
header), choosing light or dark values from the active Streamlit theme.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from pathlib import Path

import streamlit as st

ASSETS = Path(__file__).with_name("assets")
# Copies of docs/assets/agentloop-*.svg, the project's logo; the package and
# container image ship the dashboard without docs/.
LOGO_LIGHT = ASSETS / "agentloop-logo-light.svg"
LOGO_DARK = ASSETS / "agentloop-logo-dark.svg"
MARK = ASSETS / "agentloop-mark.svg"


@dataclass(frozen=True)
class Page:
    name: str
    icon: str
    description: str


PAGES = (
    Page(
        "Overview",
        ":material/space_dashboard:",
        "Runs, runtime, cost and token volume for this project.",
    ),
    Page(
        "Traces",
        ":material/timeline:",
        "Every stored run with its labels, event timeline and trace JSON.",
    ),
    Page(
        "Optimization Queue",
        ":material/format_list_numbered:",
        "Recurring findings across runs, ranked for investigation.",
    ),
    Page(
        "Optimization",
        ":material/speed:",
        "Bottlenecks, parallelizable groups and an optimization plan for one run.",
    ),
    Page(
        "Diagnosis",
        ":material/troubleshoot:",
        "Machine-actionable findings with evidence, affected spans and savings.",
    ),
    Page(
        "Patch Plan",
        ":material/build:",
        "Dry-run, framework-aware rewrite plans for a run's findings.",
    ),
    Page(
        "Replay Proof",
        ":material/compare_arrows:",
        "Before-and-after metrics, gates and a PR comment for two runs.",
    ),
    Page(
        "CI Runs",
        ":material/merge:",
        "Baseline and candidate traces uploaded by CI, compared per workflow run.",
    ),
    Page(
        "Quality Gates",
        ":material/verified:",
        "Score baseline and candidate output against quality fixtures.",
    ),
    Page(
        "Value & Pricing",
        ":material/payments:",
        "Operational value, reliability risk and a pricing scenario for a run.",
    ),
    Page(
        "Audit Log",
        ":material/history:",
        "Key changes, trace deletions and retention purges, newest first.",
    ),
    Page(
        "API Keys",
        ":material/key:",
        "Create and revoke project-scoped API keys.",
    ),
    Page(
        "Ingest",
        ":material/upload:",
        "Generate demo traces or upload trace JSON.",
    ),
    Page(
        "Setup",
        ":material/settings:",
        "Commands for local, API and Postgres setups.",
    ),
)
PAGE_NAMES = [page.name for page in PAGES]
NAV_SEPARATOR = chr(0x2002)  # en space between a page's icon and name
_BY_NAME = {page.name: page for page in PAGES}


def nav_label(name: str) -> str:
    page = _BY_NAME.get(name)
    return f"{page.icon}{NAV_SEPARATOR}{name}" if page else name


def _dark() -> bool:
    return getattr(getattr(st.context, "theme", None), "type", None) == "dark"


def _palette() -> dict[str, str]:
    if _dark():
        return {
            "card": "#221d2b",
            "card_border": "#3c3449",
            "muted": "#c2b8ce",
            "nav_hover": "rgba(199, 161, 255, 0.10)",
            "nav_active": "rgba(199, 161, 255, 0.18)",
            "nav_active_text": "#e4d2ff",
            "accent": "#c7a1ff",
            "shadow": "none",
        }
    return {
        "card": "#ffffff",
        "card_border": "#e6e0ed",
        "muted": "#61576e",
        "nav_hover": "rgba(112, 71, 186, 0.06)",
        "nav_active": "rgba(112, 71, 186, 0.11)",
        "nav_active_text": "#5a3699",
        "accent": "#7047ba",
        "shadow": "0 1px 2px rgba(37, 29, 52, 0.05)",
    }


def apply_styles() -> None:
    """Brand the sidebar and style the navigation, metric cards and headers."""
    logo = LOGO_DARK if _dark() else LOGO_LIGHT
    if logo.exists():
        st.logo(str(logo), size="large", icon_image=str(MARK) if MARK.exists() else None)
    p = _palette()
    st.markdown(
        f"""
<style>
/* Sidebar navigation: the page radio drawn as a list of links. */
section[data-testid="stSidebar"] [data-testid="stElementContainer"]:has(.stRadio),
section[data-testid="stSidebar"] .stRadio {{ width: 100%; }}
section[data-testid="stSidebar"] [data-testid="stRadioGroup"] {{
  gap: 2px; align-items: stretch; width: 100%;
}}
section[data-testid="stSidebar"] [data-testid="stRadioGroup"] > div {{ width: 100%; }}
section[data-testid="stSidebar"] [data-testid="stRadioOption"] {{
  width: 100%; margin: 0; padding: 0.42rem 0.65rem; border-radius: 0.5rem;
  transition: background-color 120ms ease;
}}
section[data-testid="stSidebar"] [data-testid="stRadioOption"] > div > div:first-child {{
  display: none;
}}
section[data-testid="stSidebar"] [data-testid="stRadioOption"]:hover {{
  background: {p["nav_hover"]};
}}
section[data-testid="stSidebar"] [data-testid="stRadioOption"][data-selected="true"] {{
  background: {p["nav_active"]};
}}
section[data-testid="stSidebar"] [data-testid="stRadioOption"][data-selected="true"] p {{
  color: {p["nav_active_text"]}; font-weight: 600;
}}
section[data-testid="stSidebar"] [data-testid="stRadioOption"] p {{ font-size: 0.92rem; }}

/* Metric cards. */
[data-testid="stMetric"] {{
  background: {p["card"]}; border: 1px solid {p["card_border"]};
  border-radius: 0.75rem; padding: 0.9rem 1.1rem; box-shadow: {p["shadow"]};
}}
[data-testid="stMetricLabel"] p {{
  color: {p["muted"]}; font-size: 0.8rem; font-weight: 600;
  letter-spacing: 0.02em; text-transform: uppercase;
}}

/* Page header. */
.al-header {{ margin: 0 0 1.25rem 0; }}
.al-eyebrow {{
  color: {p["accent"]}; font-size: 0.78rem; font-weight: 700;
  letter-spacing: 0.08em; text-transform: uppercase; margin-bottom: 0.15rem;
}}
.al-header h1 {{ padding: 0 0 0.2rem 0; }}
.al-header p {{ color: {p["muted"]}; margin: 0; font-size: 0.98rem; }}

/* Breathing room between sections. */
[data-testid="stMainBlockContainer"] {{ padding-top: 2.5rem; max-width: 1280px; }}
[data-testid="stHeadingWithActionElements"] h3 {{ margin-top: 0.75rem; }}

@media (max-width: 640px) {{
  [data-testid="stMainBlockContainer"] {{ padding-top: 1.5rem; }}
  [data-testid="stMetric"] {{ padding: 0.6rem 0.85rem; }}
  [data-testid="stMetricValue"] {{ font-size: 1.4rem; }}
}}
</style>
""",
        unsafe_allow_html=True,
    )


def page_header(
    name: str, project_id: str | None = None, *, description: str | None = None
) -> None:
    """Title and one-line description for the current page."""
    page = _BY_NAME.get(name)
    if description is None:
        description = page.description if page else ""
    eyebrow = "AgentLoop" + (f" · {project_id}" if project_id else "")
    st.markdown(
        f'<div class="al-header"><div class="al-eyebrow">{html.escape(eyebrow)}</div>'
        f"<h1>{html.escape(name)}</h1><p>{html.escape(description)}</p></div>",
        unsafe_allow_html=True,
    )
