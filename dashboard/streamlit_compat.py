from __future__ import annotations

from functools import wraps
from importlib.metadata import PackageNotFoundError, version
from typing import Any

_STRETCH_WIDTH_MIN_VERSION = (1, 49)


def _streamlit_version() -> tuple[int, int]:
    try:
        raw = version("streamlit")
    except PackageNotFoundError:
        return (0, 0)
    parts = raw.split(".")
    try:
        return (int(parts[0]), int(parts[1]))
    except (IndexError, ValueError):
        return (0, 0)


def _normalize_dataframe_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Translate the modern stretch width to the older supported API."""
    normalized = dict(kwargs)
    if normalized.get("width") == "stretch":
        normalized["width"] = None
        normalized["use_container_width"] = True
    return normalized


def install_dataframe_width_compat() -> None:
    """Keep the dashboard compatible with the declared Streamlit 1.34+ floor.

    Streamlit 1.49 added string dataframe widths such as ``"stretch"``. Older
    supported releases use ``use_container_width=True`` instead. The dashboard
    package installs this adapter only for Streamlit versions below 1.49 so the
    modern API remains untouched on current releases.
    """
    if _streamlit_version() >= _STRETCH_WIDTH_MIN_VERSION:
        return

    import streamlit as st
    from streamlit.delta_generator import DeltaGenerator

    original_method = DeltaGenerator.dataframe
    if getattr(original_method, "__agentloop_width_compat__", False):
        return

    @wraps(original_method)
    def dataframe_method(self, *args, **kwargs):
        return original_method(self, *args, **_normalize_dataframe_kwargs(kwargs))

    dataframe_method.__agentloop_width_compat__ = True
    DeltaGenerator.dataframe = dataframe_method

    original_module_function = st.dataframe

    @wraps(original_module_function)
    def dataframe(*args, **kwargs):
        return original_module_function(*args, **_normalize_dataframe_kwargs(kwargs))

    dataframe.__agentloop_width_compat__ = True
    st.dataframe = dataframe
