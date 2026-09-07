"""Entry point for running the MCP server as a module.

Usage:
    python -m mcp_server
    MCP_TRANSPORT=stdio python -m mcp_server

Environment variables:
    COCKPIT_API_URL: Base URL for cockpit API (default: http://localhost:8085)
"""

from mcp_server.run import main

if __name__ == "__main__":
    main()
