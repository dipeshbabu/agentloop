from __future__ import annotations

import streamlit as st
from streamlit.delta_generator import DeltaGenerator

from dashboard import streamlit_compat


def test_stretch_width_normalizes_to_legacy_container_width() -> None:
    result = streamlit_compat._normalize_dataframe_kwargs({"width": "stretch", "hide_index": True})

    assert result == {"width": None, "use_container_width": True, "hide_index": True}


def test_non_stretch_width_is_unchanged() -> None:
    assert streamlit_compat._normalize_dataframe_kwargs({"width": 640}) == {"width": 640}


def test_old_streamlit_path_installs_both_dataframe_adapters(monkeypatch) -> None:
    original_method = DeltaGenerator.dataframe
    original_module_function = st.dataframe
    monkeypatch.setattr(streamlit_compat, "_streamlit_version", lambda: (1, 48))

    try:
        streamlit_compat.install_dataframe_width_compat()

        assert getattr(DeltaGenerator.dataframe, "__agentloop_width_compat__", False) is True
        assert getattr(st.dataframe, "__agentloop_width_compat__", False) is True
    finally:
        DeltaGenerator.dataframe = original_method
        st.dataframe = original_module_function


def test_declared_dashboard_floor_remains_supported() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")

    assert 'dashboard = ["streamlit>=1.34.0"' in pyproject
