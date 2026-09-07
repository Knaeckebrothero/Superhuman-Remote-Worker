"""Compatibility readers for Officer blocks in thread metadata.

These retain the route helpers' JSONB parsing semantics. They identify a
candidate, not its authority: durable post validation belongs to officer_admission.
"""

import json


def thread_officer_meta(thread: dict) -> dict:
    """The officer block from a thread row's metadata.config_override, or {}."""
    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    officer_meta = (metadata.get("config_override") or {}).get("officer") or {}
    return officer_meta if isinstance(officer_meta, dict) else {}


def officer_meta_enabled(officer_meta: dict) -> bool:
    return officer_meta.get("enabled") in (True, "true", "True", 1)
