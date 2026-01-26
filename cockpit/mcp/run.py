#!/usr/bin/env python3
"""Entry script for the MCP server.

Supports dual transport modes:
- stdio: For local development with Claude Code
- http: For Kubernetes / remote deployment

Usage:
    # stdio (default, for local development)
    python run.py

    # http (for container deployment)
    MCP_TRANSPORT=http MCP_PORT=8000 python run.py

Environment variables:
    COCKPIT_API_URL: Base URL for cockpit API (default: http://localhost:8085)
    MCP_TRANSPORT: Transport mode - "stdio" or "http" (default: stdio)
    MCP_HOST: HTTP server host (default: 0.0.0.0)
    MCP_PORT: HTTP server port (default: 8000)
"""

import os
import sys
from pathlib import Path

# Add parent directory to allow relative imports
sys.path.insert(0, str(Path(__file__).parent))

from server import mcp  # noqa: E402


def main():
    """Run the MCP server with configured transport."""
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()

    if transport == "http":
        # Kubernetes / remote deployment
        host = os.environ.get("MCP_HOST", "0.0.0.0")
        port = int(os.environ.get("MCP_PORT", "8000"))
        print(f"Starting MCP server on http://{host}:{port}")
        print(f"Health check: http://{host}:{port}/health")
        print(f"MCP endpoint: http://{host}:{port}/mcp/")
        mcp.run(transport="streamable-http", host=host, port=port)
    else:
        # Local development (Claude Code via stdio)
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
