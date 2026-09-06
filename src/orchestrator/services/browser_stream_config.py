"""Fail-closed configuration for the shared-browser stream broker."""

from __future__ import annotations

import os
from dataclasses import dataclass

_TRUTHY = frozenset({"1", "true", "yes", "on"})


class BrowserStreamConfigurationError(ValueError):
    """Raised when shared-browser stream configuration is unusable."""


def _truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUTHY


def _bounded_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise BrowserStreamConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise BrowserStreamConfigurationError(
            f"{name} must be within [{minimum}, {maximum}]"
        )
    return value


@dataclass(frozen=True, slots=True)
class BrowserStreamConfig:
    enabled: bool
    stream_port: int
    max_viewers: int
    connect_timeout_seconds: int
    activity_interval_seconds: int


def browser_stream_config() -> BrowserStreamConfig:
    """Load and validate broker settings from the current environment."""
    return BrowserStreamConfig(
        enabled=_truthy("CANVAS_SHARED_BROWSER_ENABLED"),
        stream_port=_bounded_int(
            "CANVAS_BROWSER_STREAM_PORT",
            38801,
            minimum=1024,
            maximum=65535,
        ),
        max_viewers=_bounded_int(
            "CANVAS_BROWSER_MAX_VIEWERS",
            3,
            minimum=1,
            maximum=16,
        ),
        connect_timeout_seconds=_bounded_int(
            "CANVAS_BROWSER_CONNECT_TIMEOUT_SECONDS",
            10,
            minimum=1,
            maximum=120,
        ),
        activity_interval_seconds=_bounded_int(
            "CANVAS_BROWSER_ACTIVITY_INTERVAL_SECONDS",
            60,
            minimum=10,
            maximum=3600,
        ),
    )


__all__ = [
    "BrowserStreamConfig",
    "BrowserStreamConfigurationError",
    "browser_stream_config",
]
