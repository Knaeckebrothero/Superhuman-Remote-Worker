"""Agent API client for health checks and status monitoring."""

import os
from typing import Any

import requests


DEFAULT_TIMEOUT = 5.0


def get_agent_urls() -> dict[str, str]:
    """Get agent URLs from environment."""
    return {
        "creator": os.environ.get("CREATOR_AGENT_URL", "http://localhost:8001"),
        "validator": os.environ.get("VALIDATOR_AGENT_URL", "http://localhost:8002"),
    }


def check_health(agent_url: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any] | None:
    """Check agent health via GET /health endpoint.

    Returns health response dict or None if agent is unreachable.
    """
    try:
        response = requests.get(f"{agent_url}/health", timeout=timeout)
        if response.status_code == 200:
            return response.json()
        return {"status": "unhealthy", "code": response.status_code}
    except requests.RequestException:
        return None


def get_status(agent_url: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any] | None:
    """Get detailed agent status via GET /status endpoint.

    Returns status response dict or None if agent is unreachable.
    """
    try:
        response = requests.get(f"{agent_url}/status", timeout=timeout)
        if response.status_code == 200:
            return response.json()
        return None
    except requests.RequestException:
        return None


def get_all_agent_status() -> dict[str, dict[str, Any]]:
    """Get health and status for all configured agents."""
    urls = get_agent_urls()
    result = {}

    for name, url in urls.items():
        health = check_health(url)
        status = get_status(url) if health else None

        result[name] = {
            "url": url,
            "online": health is not None and health.get("status") != "unhealthy",
            "health": health,
            "status": status,
        }

    return result
