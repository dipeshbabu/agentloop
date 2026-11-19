"""Shared Streamlit ``AppTest`` timeout for dashboard tests.

The first script run in a process imports Streamlit and the dashboard, which takes a
few seconds alone but can exceed a minute on a loaded machine or a busy CI runner.
A timeout there is not a dashboard failure, so allow a generous default that can be
raised further with ``AGENTLOOP_APPTEST_TIMEOUT`` (seconds).
"""

from __future__ import annotations

import os

APPTEST_TIMEOUT = float(os.environ.get("AGENTLOOP_APPTEST_TIMEOUT", "180"))
