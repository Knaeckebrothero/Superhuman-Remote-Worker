"""Deterministic naming for MCP-provided tools.

Names use ``mcp__<server_slug>__<tool>``. The registry retains the mapping
to the server's original tool, while this module enforces the 64-character
limit used by OpenAI-compatible function-calling APIs.
"""

import hashlib
import re

_MAX_TOOL_NAME = 64
_MAX_SLUG = 16


def mcp_server_slug(name: str, max_len: int = _MAX_SLUG) -> str:
    """Return a lowercase, underscore-separated server slug."""
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    slug = slug[:max_len].rstrip("_")
    return slug or "server"


def namespace_mcp_tool(
    server_slug: str,
    tool_name: str,
    taken: set[str],
) -> str:
    """Build a deterministic, unique namespaced tool name of at most 64 chars."""
    clean_tool = re.sub(r"[^a-zA-Z0-9_-]+", "_", tool_name).strip("_") or "tool"
    base = f"mcp__{server_slug}__{clean_tool}"
    if len(base) <= _MAX_TOOL_NAME and base not in taken:
        return base

    digest = hashlib.sha1(f"{server_slug}:{tool_name}".encode()).hexdigest()[:4]
    trimmed = base[: _MAX_TOOL_NAME - 5].rstrip("_")
    candidate = f"{trimmed}_{digest}"
    while candidate in taken:
        digest = hashlib.sha1(candidate.encode()).hexdigest()[:4]
        candidate = f"{trimmed}_{digest}"
    return candidate
