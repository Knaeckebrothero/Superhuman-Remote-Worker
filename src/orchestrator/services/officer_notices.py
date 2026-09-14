"""Telling the Officer, and telling the user a route fell back.

Extracted verbatim from ``orchestrator.main`` (R1.B07 lane O, census group
``S_OFFICER``). Two one-way deliveries the Post lifecycle and the conference
machinery both need:

* :func:`inject_officer_notice` — a best-effort one-liner through the agent's
  ``/api/input``. It **bypasses holds by design**: Legate input always reaches
  him. Never raises.
* :func:`deliver_staged_officer_routes` — durable route fallback intents,
  delivered only *after* their transaction committed. A failed dispatch leaves
  ``user_delivery_at`` null, which is the routing reconciler's retry contract,
  so delivery can never roll back or falsify a hold/decommission transition.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass
class OfficerNoticeDependencies:
    """The store both deliveries read and write through."""

    store: Any


async def inject_officer_notice(
    officer_thread: dict[str, Any],
    text: str,
    *,
    dependencies: OfficerNoticeDependencies,
) -> bool:
    """Best-effort one-liner via the agent's /api/input (bypasses holds by
    design — Legate input always reaches him). Never raises."""
    try:
        from orchestrator.services import session_wake as _sw

        agent = await _sw._resolve_live_agent(dependencies.store, officer_thread)
        if agent is None:
            return False
        outcome = await _sw._inject_live(
            agent,
            text,
            delivery_id=str(uuid4()),
            db=dependencies.store,
        )
        return outcome == _sw.WakeDeliveryResult.EXECUTED
    except Exception:
        logger.debug("officer notice: inject failed (non-fatal)", exc_info=True)
        return False


async def deliver_staged_officer_routes(
    routes: list[dict[str, Any]],
    *,
    reason: str,
    dependencies: OfficerNoticeDependencies,
) -> int:
    """Deliver durable route fallback intents after their transaction commits.

    A failed dispatch leaves ``user_delivery_at`` null, which is the routing
    reconciler's retry contract. Delivery can therefore never roll back or
    falsify a hold/decommission transition.
    """
    if not routes:
        return 0
    from orchestrator.services import message_routing as _routing_svc

    delivered = 0
    for route in routes:
        try:
            if await _routing_svc.deliver_route_to_user(
                dependencies.store, route, reason=reason
            ):
                delivered += 1
        except Exception:
            logger.warning(
                "Officer route %s delivery failed after durable %s transition; "
                "leaving it retryable",
                str(route.get("route_id") or "")[:8],
                reason,
                exc_info=True,
            )
    return delivered


__all__ = [
    "OfficerNoticeDependencies",
    "deliver_staged_officer_routes",
    "inject_officer_notice",
]
