"""Lazy access to the installed MCP SDK.

The application lives in ``mcp_server`` and cannot shadow the SDK's ``mcp``
package. Python's import machinery owns module identity and synchronization.
"""

from __future__ import annotations

from importlib import import_module
from types import ModuleType


def ensure_mcp_sdk() -> ModuleType:
    """Return the canonical third-party MCP package."""
    return import_module("mcp")
