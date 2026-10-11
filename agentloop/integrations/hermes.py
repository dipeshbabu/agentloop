"""Hermes hook registration and dependency-free directory-plugin packaging."""

from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path

from agentloop.integrations import hermes_recorder
from agentloop.integrations.hermes_recorder import HermesRecorder, register
from agentloop.version import __version__


def register_hermes(ctx) -> HermesRecorder:
    """Register the profile-scoped observer once on a Hermes PluginContext."""
    register(ctx)
    return ctx._agentloop_hermes_recorder


def export_hermes_plugin(directory: str | Path) -> Path:
    """Create a fresh standalone plugin; never install or enable it in Hermes."""
    out = Path(directory)
    if out.exists() or out.is_symlink():
        raise ValueError("Plugin output directory already exists; choose a fresh path")
    recorder_path = Path(hermes_recorder.__file__)
    if not recorder_path.is_file():
        raise ValueError("Plugin export requires a Python package installation or source checkout")
    source = recorder_path.read_bytes()
    distribution = importlib.metadata.distribution("agentloop-profiler")
    license_text = distribution.read_text("licenses/LICENSE")
    if license_text is None:
        license_text = (Path(__file__).resolve().parents[2] / "LICENSE").read_text(encoding="utf-8")
    manifest = {
        "name": "agentloop",
        "version": __version__,
        "manifest_version": 2,
        "api_version": 1,
        "description": "Local AgentLoop traces for Hermes model attempts, tools and turn outcomes.",
        "kind": "standalone",
        "license": "Apache-2.0",
        "homepage": "https://github.com/dipeshbabu/agentloop",
        "requires_hermes": ">=0.21.6,<0.22",
        "provides_hooks": list(hermes_recorder.HOOKS),
        "config_schema": {
            "export_dir": {
                "type": "str",
                "default": "agentloop/traces",
                "description": "Trace directory, relative to the active Hermes profile home or absolute.",
            },
            "capture_content": {
                "type": "bool",
                "default": False,
                "description": "Retain bounded tool/model/final output text; prompts and arguments remain excluded.",
            },
            "max_active_turns": {"type": "int", "default": 64},
            "max_events": {"type": "int", "default": 2048},
        },
    }
    files = {
        "plugin.yaml": json.dumps(manifest, indent=2).encode(),
        "__init__.py": b'"""AgentLoop Hermes observer plugin (Apache-2.0)."""\nfrom .recorder import register\n',
        "recorder.py": source,
        "LICENSE": license_text.encode("utf-8"),
    }
    written = []
    out.mkdir(parents=True, exist_ok=False)
    try:
        for name, content in files.items():
            target = out / name
            with target.open("xb") as handle:
                written.append(target)
                handle.write(content)
    except BaseException:
        for path in reversed(written):
            path.unlink(missing_ok=True)
        out.rmdir()
        raise
    return out
