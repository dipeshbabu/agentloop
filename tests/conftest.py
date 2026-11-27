"""Shared test setup."""

import os

# The CLI builds its Rich console at import time, and FORCE_COLOR makes it
# emit ANSI codes even into the test runner. Tests match plain text, so a
# shell that forces colour would otherwise fail them; clear it before any
# test module imports the CLI.
os.environ.pop("FORCE_COLOR", None)
