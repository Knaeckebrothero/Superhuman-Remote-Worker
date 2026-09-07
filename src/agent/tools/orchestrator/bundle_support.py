"""Shared bundle encoding and workspace I/O for orchestrator authoring tools.

Tool-specific field allowlists and admission remain with their callers.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional

import httpx

from agent.tools.context import ToolContext
from shared.orch_surface.formatters import truncate_text as _truncate


def _safe_limit(limit: int, *, default: int, maximum: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, maximum))


def _canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _bundle_hash(bundle: Dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(bundle).encode("utf-8")).hexdigest()


def _pretty_json(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=False, ensure_ascii=True)


def _http_error(e: httpx.HTTPStatusError) -> str:
    detail = ""
    try:
        detail = e.response.text
    except Exception:
        detail = ""
    if detail:
        return f"HTTP {e.response.status_code}: {_truncate(detail, limit=500)}"
    return f"HTTP {e.response.status_code}"


def _write_workspace_json(
    context: ToolContext,
    destination_path: Optional[str],
    payload: Dict[str, Any],
) -> Optional[str]:
    if not destination_path:
        return None
    if not context.workspace_manager:
        raise ValueError("destination_path requires a workspace-backed session.")
    context.workspace_manager.write_file(destination_path, _pretty_json(payload) + "\n")
    return destination_path


def _read_bundle_payload(
    context: ToolContext,
    *,
    bundle: Optional[Dict[str, Any]],
    bundle_path: Optional[str],
) -> Dict[str, Any]:
    if bundle is not None and bundle_path:
        raise ValueError("Provide either bundle or bundle_path, not both.")
    if bundle_path:
        if not context.workspace_manager:
            raise ValueError("bundle_path requires a workspace-backed session.")
        text = context.workspace_manager.read_file(bundle_path)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"{bundle_path} is not valid JSON: {e}") from e
        if not isinstance(parsed, dict):
            raise ValueError("Bundle JSON must be an object.")
        return parsed
    if bundle is None:
        raise ValueError("bundle or bundle_path is required.")
    if not isinstance(bundle, dict):
        raise ValueError("bundle must be a JSON object.")
    return bundle


def _unwrap_bundle(payload: Dict[str, Any]) -> Dict[str, Any]:
    nested = payload.get("bundle")
    if isinstance(nested, dict):
        return nested
    return payload
