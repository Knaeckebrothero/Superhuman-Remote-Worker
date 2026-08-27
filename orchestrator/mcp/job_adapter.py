"""Thin FastMCP adapter for the shared job descriptor inventory."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from src.shared.orch_surface.client import AsyncCockpitClient
from src.shared.orch_surface.jobs import (
    CallerCtx,
    JobToolResult,
    JOB_DESCRIPTORS,
    make_bound_handler,
)


def _mcp_content_types() -> tuple[type[Any], type[Any]]:
    """Load native content types only when a typed result is returned.

    Importing the SDK at adapter-module import time poisons ``sys.modules['mcp']``
    for repository processes that deliberately load ``orchestrator/mcp`` as the
    top-level package. String-only tool registration needs no SDK content type,
    so keep the collision resolver behind the actual image boundary.
    """
    try:
        from mcp.types import ImageContent, TextContent
    except ImportError:
        from src.tools.mcp.sdk import ensure_mcp_sdk

        ensure_mcp_sdk()
        from mcp.types import ImageContent, TextContent
    return ImageContent, TextContent


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

        def _mcp_result(result: str | JobToolResult) -> Any:
            if isinstance(result, str):
                return result
            image_content_type, text_content_type = _mcp_content_types()
            content: list[Any] = [text_content_type(type="text", text=result.text)]
            if result.image is not None:
                content.append(
                    image_content_type(
                        type="image",
                        data=result.image.base64_data,
                        mimeType=result.image.media_type,
                    )
                )
            return content

        function = make_bound_handler(
            item,
            client_provider=client_provider,
            caller_provider=caller_provider,
            result_adapter=_mcp_result,
        )
        registered[item.name] = mcp.tool(
            function,
            name=item.name,
            annotations=contract.annotations,
            meta={"io.srw.capability": contract.metadata()},
        )
    return registered


__all__ = ["register_job_tools"]
