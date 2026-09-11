"""Request bodies for the notification feed.

Moved verbatim from ``orchestrator.main`` (R1.B07 lane N, census group
``J_notifications``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class NotificationSeenRequest(BaseModel):
    ids: list[str]


class NotificationActRequest(BaseModel):
    action_type: str
    params: dict[str, Any] = {}


__all__ = ["NotificationActRequest", "NotificationSeenRequest"]
