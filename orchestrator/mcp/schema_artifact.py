"""Generate the canonical MCP ``tools/list`` artifact used by image CI."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

try:
    from .server import canonical_tool_schema
except ImportError:
    from server import canonical_tool_schema  # type: ignore[no-redef]


async def _write(output: Path) -> None:
    tools, digest = await canonical_tool_schema()
    document = {"digest": digest, "tools": tools}
    output.write_text(
        json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python schema_artifact.py OUTPUT.json")
    asyncio.run(_write(Path(sys.argv[1])))


if __name__ == "__main__":
    main()
