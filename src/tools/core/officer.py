"""Officer (centurion) sleep tool — the always-on session's park verb.

``sleep(minutes, reason)`` is how an officer session ends a wake: it records a
wake request on the ToolContext and the turn loop ends the turn after the
current tool batch instead of paying another LLM iteration
(persistent_graph.py, officer sleep check). The transport then consumes the
request at park time and FILES the wake with the orchestrator as a durable
``timer`` outbox row — the timer is Postgres-owned, so a pod crash or node
downtime never loses the schedule (knowledge-base/knowledge/features/centurion.md §4, decision
2026-07-29). The tool itself is a pure flag-setter, mirroring the
``request_workspace_upgrade`` freeze-seam pattern in ``upgrade.py``.

Clamping to ``officer.sleep_min/max_minutes`` is authoritative on the
orchestrator side; the value passed here is a request. Events always wake the
officer early — sleeping never risks missing anything.

Category ``core`` (not an execution category) so it survives
``filter_tools_by_backend`` on the ``none`` lite tier officers run on. Only
exposed when ``officer.enabled`` (persistent_session tool assembly).
"""

import logging
from datetime import datetime, timezone
from typing import Any, List

from langchain_core.tools import tool

from ..context import ToolContext

from src.shared.tool_catalog.definitions import (
    OFFICER_TOOLS_METADATA as OFFICER_TOOLS_METADATA,
)

logger = logging.getLogger(__name__)


def create_officer_tools(context: ToolContext) -> List[Any]:
    """Create the officer sleep tool.

    No workspace/todo dependency — it only records a wake request on the
    ToolContext, so it loads on the ``none`` lite tier (``todo_manager=None``).
    """

    @tool
    async def sleep(minutes: int, reason: str) -> str:
        """End this wake and sleep until the timer fires or an event arrives.

        Call this as the LAST action of a wake, once you have judged the
        sitrep and taken whatever actions it warranted. Choose the duration
        deliberately: long when everything is healthy or you are waiting on a
        long-running job, short right after dispatching something risky. The
        configured min/max bounds are applied on the orchestrator side.

        Sleeping is safe: any event — a job completing or failing, a message
        from the user, a permission request — wakes you before the timer. The
        timer itself is durable (it survives restarts and downtime), so you
        will always be woken even if nothing happens.

        Args:
            minutes: Requested sleep duration in minutes.
            reason: One short line on why this duration (shown in the wake
                message and the officer's log, e.g. "3 jobs healthy, waiting
                on the migration job").

        Returns:
            Confirmation that the wake-up call has been filed.
        """
        requested = int(minutes)
        context.request_officer_sleep(
            {
                "minutes": requested,
                "reason": reason or "",
                "requested_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        logger.info("officer sleep requested: minutes=%d reason=%r", requested, reason)
        return (
            f"Wake-up call filed for ~{requested} minutes (bounds applied "
            "server-side). This turn ends now; events will wake you earlier."
        )

    @tool
    async def notify_user(message: str, urgency: str = "log", subject: str = "") -> str:
        """Message your Legate (the user) through the notify contract.

        Three tiers — pick the LOWEST that serves the purpose:
          * ``log``: for the record only. Costs nothing, interrupts nobody.
            The default; most observations belong here.
          * ``digest``: lands on the Legate's notification center to be read
            at their next look. For things the Legate should know but not be
            woken for.
          * ``page``: reaches the Legate now, out of band (email/push per
            their preferences). For things that cannot wait: repeated
            failures you cannot fix, a blocked decision above your authority,
            capacity exhausted with work queued. The platform throttles
            repeats (identical text on one day is one notification), but a
            page still interrupts a human — spend them like the scarce
            resource they are.

        This is also how you answer a Legate note when the Legate is not live
        in your session: ``digest`` for an answer, ``page`` only if the
        answer cannot wait.

        Args:
            message: What the Legate needs to know, in 1-5 sentences.
            urgency: 'log' | 'digest' | 'page'.
            subject: Short subject line (page/digest only).

        Returns:
            How the message was delivered.
        """
        # Local import: the shared client helpers live in the orchestrator
        # tool family; importing at call time keeps this module free of an
        # import-time httpx/env dependency for the sleep-only tests.
        from ..orchestrator.jobs import _get_client, _get_orchestrator_url

        thread_id = getattr(context, "thread_id", None)
        if not thread_id:
            return "notify_user unavailable: no thread id on this session."
        url = f"{_get_orchestrator_url()}/api/agents/threads/{thread_id}/officer/notify"
        try:
            async with _get_client(user_id=getattr(context, "user_id", None)) as client:
                resp = await client.post(
                    url,
                    json={
                        "message": str(message),
                        "urgency": str(urgency or "log"),
                        "subject": str(subject or ""),
                    },
                )
        except Exception as e:
            logger.warning("notify_user failed (non-fatal): %s", e)
            return f"notify_user failed to reach the orchestrator: {e}"
        if resp.status_code != 200:
            return f"notify_user rejected ({resp.status_code}): {resp.text[:200]}"
        data = resp.json()
        delivered = data.get("delivered")
        if delivered == "page":
            return "Paged the Legate (notification recorded; reaches them now)."
        if delivered == "digest":
            return "Recorded on the Legate's notification center for their next look."
        return "Logged."

    return [sleep, notify_user]
