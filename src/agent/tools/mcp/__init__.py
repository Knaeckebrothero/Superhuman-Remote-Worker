"""MCP client toolkit for external MCP servers attached as datasources.

The worker agent acts as an MCP client: user-owned ``type='mcp'``
datasources are connected at job or session start and their tools are
registered dynamically.
"""

from agent.tools.mcp.manager import MCPManager, parse_mcp_config
from agent.tools.mcp.naming import mcp_server_slug, namespace_mcp_tool

__all__ = [
    "MCPManager",
    "mcp_server_slug",
    "namespace_mcp_tool",
    "parse_mcp_config",
]
