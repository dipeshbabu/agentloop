"""Render every dashboard page and fail on any exception.

Streamlit's /_stcore/health endpoint answers even when the app script raises,
so a deployment can look healthy while every page shows an error. This runs
the real script with Streamlit's test runner, in the environment it is
deployed in (for example inside the dashboard container), and checks each
page renders without an exception.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--app", type=Path, default=ROOT / "dashboard" / "app.py")
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()

    app = AppTest.from_file(str(args.app), default_timeout=args.timeout)
    app.run()
    if app.exception:
        print(f"The dashboard failed to start: {app.exception[0].value}", file=sys.stderr)
        return 1
    if not app.sidebar.radio:
        # Project sign-in mode shows only the sign-in form until a key is entered.
        print("Dashboard rendered its sign-in page without errors")
        return 0
    failures = []
    pages = list(app.sidebar.radio[0].options)
    for page in pages:
        app.sidebar.radio[0].set_value(page).run()
        if app.exception:
            failures.append(f"{page}: {app.exception[0].value}")
    if failures:
        print("Dashboard pages raised errors:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print(f"All {len(pages)} dashboard pages rendered without errors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
