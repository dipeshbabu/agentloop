from __future__ import annotations

import os


def get_api_key() -> str | None:
    return os.getenv("AGENTLOOP_API_KEY")


def get_admin_api_key() -> str | None:
    return os.getenv("AGENTLOOP_ADMIN_API_KEY")


def require_api_key() -> bool:
    return os.getenv("AGENTLOOP_REQUIRE_API_KEY", "false").lower() in {"1", "true", "yes", "on"}


def get_api_url() -> str:
    return os.getenv("AGENTLOOP_API_URL", "http://127.0.0.1:8000")


def get_cors_origins() -> list[str]:
    value = os.getenv("AGENTLOOP_CORS_ORIGINS", "")
    return [origin.strip() for origin in value.split(",") if origin.strip()]
