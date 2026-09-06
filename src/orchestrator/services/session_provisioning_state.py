"""Helpers for coordinating concurrent session agent provisioning."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any


_ACTIVE_AGENT_POD_STATUSES = frozenset(
    {
        "creating",
        "created",
        "pending",
        "starting",
        "ready",
    }
)
_TERMINAL_AGENT_POD_STATUSES = frozenset(
    {
        "completed",
        "deleted",
        "failed",
        "terminated",
    }
)


def _default_marker_ttl_s() -> int:
    raw = os.environ.get(
        "AGENT_PROVISION_MARKER_TTL_S",
        os.environ.get("AGENT_BIND_TIMEOUT_S", "300"),
    )
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 300


def _coerce_metadata(thread: dict[str, Any]) -> dict[str, Any]:
    metadata = thread.get("metadata") or {}
    if isinstance(metadata, dict):
        return metadata
    if isinstance(metadata, str):
        try:
            parsed = json.loads(metadata)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def agent_pod_provisioning_in_progress(
    thread: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    ttl_s: int | None = None,
) -> bool:
    """Return True when another path recently started a session agent pod.

    This is intentionally timestamp-gated. Legacy or corrupted metadata
    should not block retries forever; only recent, non-terminal pod markers
    are treated as an in-flight provisioning attempt.
    """
    if not thread or thread.get("agent_id"):
        return False

    marker = _coerce_metadata(thread).get("agent_pod") or {}
    if not isinstance(marker, dict):
        return False

    status = str(marker.get("status") or "").strip().lower()
    if status in _TERMINAL_AGENT_POD_STATUSES:
        return False
    if status and status not in _ACTIVE_AGENT_POD_STATUSES:
        return False
    if not status and not marker.get("pod_name"):
        return False

    timestamp = (
        marker.get("created_at") or marker.get("updated_at") or marker.get("started_at")
    )
    started_at = _parse_datetime(timestamp)
    if started_at is None:
        return False

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age_s = (current.astimezone(timezone.utc) - started_at).total_seconds()
    return age_s <= (ttl_s if ttl_s is not None else _default_marker_ttl_s())
