#!/usr/bin/env python3
"""Entry script for the MCP server.

Avoids module name conflicts with the 'mcp' package.

Usage:
    python run.py

Environment variables:
    COCKPIT_API_URL: Base URL for cockpit API (default: http://localhost:8085)
"""

import asyncio
import sys
from pathlib import Path

# Add parent directory to allow relative imports
sys.path.insert(0, str(Path(__file__).parent))

from server import main  # noqa: E402

if __name__ == "__main__":
    asyncio.run(main())
