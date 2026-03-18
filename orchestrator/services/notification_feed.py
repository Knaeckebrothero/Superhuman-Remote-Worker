"""SSE notification feed for real-time cockpit updates.

Provides per-user Server-Sent Events broadcasting for live notification
delivery to the cockpit UI. Follows the SudoGateService SSE pattern.

Event types:
    new_message      — Agent sent a new message to the user
    reply_delivered   — User's reply was routed to the agent
"""

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class NotificationFeedService:
    """Per-user SSE broadcast service for notification events."""

    def __init__(self) -> None:
        # user_id -> list of asyncio.Queue
        self._user_queues: dict[str, list[asyncio.Queue]] = {}

    def subscribe_sse(self, user_id: str) -> asyncio.Queue:
        """Create a new SSE subscription queue for a user.

        Returns a Queue that receives event dicts.
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        if user_id not in self._user_queues:
            self._user_queues[user_id] = []
        self._user_queues[user_id].append(q)
        logger.debug("SSE subscriber added for user %s (total: %d)",
                      user_id, len(self._user_queues[user_id]))
        return q

    def unsubscribe_sse(self, user_id: str, q: asyncio.Queue) -> None:
        """Remove an SSE subscription queue."""
        queues = self._user_queues.get(user_id, [])
        try:
            queues.remove(q)
        except ValueError:
            pass
        if not queues:
            self._user_queues.pop(user_id, None)

    def broadcast(
        self,
        user_id: str,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        """Push an event to all SSE clients for a specific user."""
        queues = self._user_queues.get(user_id, [])
        if not queues:
            return

        event = {"type": event_type, **data}
        dead: list[asyncio.Queue] = []

        for q in queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)

        for q in dead:
            try:
                queues.remove(q)
            except ValueError:
                pass

    @property
    def active_connections(self) -> int:
        """Total number of active SSE connections across all users."""
        return sum(len(qs) for qs in self._user_queues.values())


# Module-level singleton
notification_feed = NotificationFeedService()
