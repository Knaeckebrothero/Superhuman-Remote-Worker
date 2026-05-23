"""Shared session-lifecycle helpers for the session-start paths.

The orchestrator has two paths that move a thread through its startup
phases: the cold path in ``routers/sessions.py::_do_prepare`` (POST
/api/sessions/{tid}/prepare) and the warm-pool fast path inside
``main.py::create_thread`` (``_provision_or_assign``). Both paths must
emit the same ``session.lifecycle`` events so the cockpit's startup card
renders identically regardless of which path bound the agent.

Single source of truth for:
  - ``emit``: broadcasting ``session.lifecycle`` events on the user's
    notification channel
  - ``wait_for_binding``: polling the DB until ``threads.agent_id`` is set
  - ``wait_for_ready``: polling the agent pod's ``/ready`` endpoint until
    the pod reports session-ready (gated by the agent-side
    ``_session_ready()`` 3-way check)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from services.notification_feed import notification_feed

logger = logging.getLogger(__name__)


def emit(user_id: str, thread_id: str, state: str, **extra: Any) -> None:
    """Broadcast a ``session.lifecycle`` event to the user's SSE feed.

    Safe to call from any async context — ``notification_feed.broadcast``
    is non-blocking and silently drops events when no subscriber exists.
    """
    notification_feed.broadcast(
        user_id,
        "session.lifecycle",
        {"thread_id": thread_id, "state": state, **extra},
    )


async def wait_for_binding(thread_id: str, timeout_s: int) -> bool:
    """Poll the DB until ``threads.agent_id`` is set, or wall-clock timeout."""
    from main import postgres_db  # type: ignore

    deadline = asyncio.get_event_loop().time() + timeout_s
    interval = 2
    while asyncio.get_event_loop().time() < deadline:
        thread = await postgres_db.get_thread(thread_id)
        if thread and thread.get("agent_id"):
            return True
        await asyncio.sleep(interval)
    return False


async def wait_for_ready(pod_ip: str, pod_port: int, timeout_s: int) -> bool:
    """Poll the agent pod's /ready until it returns ready=true, or timeout.

    The agent's ``/ready`` returns ``ready=true`` only once
    ``_session_ready()`` passes its 3-way check (session attached,
    LLM tools wired, loop queue initialized). Mid-attach windows
    correctly return False, so this is the truthful signal for "the
    cockpit's WS will succeed if opened now."
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    interval = 2
    while asyncio.get_event_loop().time() < deadline:
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                resp = await client.get(f"http://{pod_ip}:{pod_port}/ready")
                if resp.status_code == 200 and resp.json().get("ready"):
                    return True
        except Exception:
            pass
        await asyncio.sleep(interval)
    return False
