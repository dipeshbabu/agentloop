from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass
class AgentLoopRuntimeConfig:
    api_url: str = "http://127.0.0.1:8000"
    api_key: str | None = None
    project_id: str = "default"
    auto_upload: bool = False
    auto_store: bool = False
    fail_silently: bool = True
    export_dir: Path | None = None


_runtime = AgentLoopRuntimeConfig()
_last_error: str | None = None


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in _TRUE_VALUES


def init(
    *,
    api_url: str | None = None,
    api_key: str | None = None,
    project_id: str | None = None,
    auto_upload: bool | None = None,
    auto_store: bool | None = None,
    fail_silently: bool | None = None,
    export_dir: str | Path | None = None,
) -> AgentLoopRuntimeConfig:
    """Configure AgentLoop once at process startup.

    Examples:

    ```python
    import agentloop

    agentloop.init(api_key="al_xxx", project_id="demo", auto_upload=True)
    ```

    Environment variables are also supported:
    - `AGENTLOOP_API_URL`
    - `AGENTLOOP_API_KEY`
    - `AGENTLOOP_PROJECT_ID`
    - `AGENTLOOP_AUTO_UPLOAD`
    - `AGENTLOOP_AUTO_STORE`
    - `AGENTLOOP_EXPORT_DIR`
    """

    global _runtime
    _runtime = AgentLoopRuntimeConfig(
        api_url=api_url or os.getenv("AGENTLOOP_API_URL", _runtime.api_url),
        api_key=api_key if api_key is not None else os.getenv("AGENTLOOP_API_KEY", _runtime.api_key),
        project_id=project_id or os.getenv("AGENTLOOP_PROJECT_ID", _runtime.project_id),
        auto_upload=auto_upload if auto_upload is not None else _env_bool("AGENTLOOP_AUTO_UPLOAD", _runtime.auto_upload),
        auto_store=auto_store if auto_store is not None else _env_bool("AGENTLOOP_AUTO_STORE", _runtime.auto_store),
        fail_silently=fail_silently if fail_silently is not None else _env_bool("AGENTLOOP_FAIL_SILENTLY", _runtime.fail_silently),
        export_dir=Path(export_dir) if export_dir is not None else (Path(os.getenv("AGENTLOOP_EXPORT_DIR")) if os.getenv("AGENTLOOP_EXPORT_DIR") else _runtime.export_dir),
    )
    return _runtime


def configure_from_env() -> AgentLoopRuntimeConfig:
    return init()


def get_runtime_config() -> AgentLoopRuntimeConfig:
    return _runtime


def get_last_error() -> str | None:
    return _last_error


def reset_runtime() -> None:
    global _runtime, _last_error
    _runtime = AgentLoopRuntimeConfig()
    _last_error = None


def should_auto_export() -> bool:
    return bool(_runtime.auto_upload or _runtime.auto_store or _runtime.export_dir)


def finalize_trace(trace: Any) -> dict[str, Any]:
    """Apply configured end-of-run side effects.

    Depending on runtime config, this can export a JSON file, save to the local
    persistent store, and/or upload to the hosted API. Errors are captured and
    suppressed by default so instrumentation never breaks the user's agent.
    """

    global _last_error
    result: dict[str, Any] = {"exported_path": None, "stored": False, "uploaded": False, "errors": []}

    try:
        if _runtime.export_dir is not None:
            _runtime.export_dir.mkdir(parents=True, exist_ok=True)
            out = _runtime.export_dir / f"{trace.run_id}.json"
            trace.export_json(out)
            result["exported_path"] = str(out)

        if _runtime.auto_store:
            from agentloop.store import get_store

            db = get_store()
            db.init()
            db.save_trace(trace, project_id=_runtime.project_id)
            result["stored"] = True

        if _runtime.auto_upload:
            from agentloop.client import AgentLoopClient

            client = AgentLoopClient(base_url=_runtime.api_url, api_key=_runtime.api_key)
            result["upload_response"] = client.upload_trace(trace)
            result["uploaded"] = True

    except Exception as exc:
        _last_error = str(exc)
        result["errors"].append(str(exc))
        if not _runtime.fail_silently:
            raise

    return result


# Load env-driven defaults on import without forcing users to call init().
configure_from_env()
