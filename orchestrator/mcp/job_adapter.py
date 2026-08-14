"""Thin FastMCP adapter for the shared job descriptor inventory."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from src.shared.orch_surface.client import AsyncCockpitClient
from src.shared.orch_surface.jobs import (
    CallerCtx,
    JOB_DESCRIPTORS,
    make_bound_handler,
)


def register_job_tools(
    mcp: Any,
    *,
    client_provider: Callable[[], AsyncCockpitClient],
    caller_provider: Callable[[], CallerCtx],
    capabilities: Mapping[str, Any],
) -> dict[str, Any]:
    """Register every shared job operation with its MCP risk contract."""
    registered: dict[str, Any] = {}
    for item in JOB_DESCRIPTORS:
        contract = capabilities.get(item.name)
        if contract is None:
            raise RuntimeError(
                f"MCP job tool {item.name!r} has no capability contract entry"
            )
        function = make_bound_handler(
            item,
            client_provider=client_provider,
            caller_provider=caller_provider,
        )
        registered[item.name] = mcp.tool(
            function,
            name=item.name,
            annotations=contract.annotations,
            meta={"io.srw.capability": contract.metadata()},
        )
    return registered


__all__ = ["register_job_tools"]
