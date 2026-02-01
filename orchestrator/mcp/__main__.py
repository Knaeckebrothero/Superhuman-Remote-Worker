"""Entry point for running the MCP server as a module.

Usage:
    python -m cockpit.mcp  (from project root)

For standalone usage, use run.py instead:
    cd cockpit/mcp && python run.py

Environment variables:
    COCKPIT_API_URL: Base URL for cockpit API (default: http://localhost:8085)
"""

import asyncio

try:
    from .server import main
except ImportError:
    from server import main  # type: ignore[no-redef]

if __name__ == "__main__":
    asyncio.run(main())
