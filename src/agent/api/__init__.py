"""API layer for Universal Agent.

Application factories and HTTP models retain their existing public names.
Resolve them lazily so importing a transport helper does not load the worker
application, graph, or provider stack.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent.api.app import create_app, set_config_path
    from agent.api.models import (
        JobStatus,
        HealthStatus,
        JobSubmitRequest,
        JobSubmitResponse,
        JobStatusResponse,
        HealthResponse,
        ErrorResponse,
    )

__all__ = [
    "create_app",
    "set_config_path",
    "JobStatus",
    "HealthStatus",
    "JobSubmitRequest",
    "JobSubmitResponse",
    "JobStatusResponse",
    "HealthResponse",
    "ErrorResponse",
]


def __getattr__(name: str) -> Any:
    """Load and cache an existing public export on first access."""
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name = "app" if name in {"create_app", "set_config_path"} else "models"
    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
