"""Officer (centurion) sleep tool — the always-on session's park verb.

``sleep(minutes, reason)`` is how an officer session ends a wake: it records a
wake request on the ToolContext and the turn loop ends the turn after the
current tool batch instead of paying another LLM iteration
(persistent_graph.py, officer sleep check). The transport then consumes the
request at park time and FILES the wake with the orchestrator as a durable
``timer`` outbox row — the timer is Postgres-owned, so a pod crash or node
downtime never loses the schedule (docs/features/centurion.md §4, decision
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
from typing import Any, Dict, List

from langchain_core.tools import tool

from ..context import ToolContext

logger = logging.getLogger(__name__)


OFFICER_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    "sleep": {
        "module": "core.officer",
        "function": "sleep",
        "description": (
            "End this wake and sleep for a number of minutes. Any event "
            "(job transition, user message) wakes you earlier; the timer is "
            "durable and survives restarts. Officer sessions only."
        ),
        "category": "core",
        "short_description": "End the wake; sleep until the timer or an event.",
        "phases": ["strategic", "tactical"],  # phase-free in sessions
    },
}


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

    return [sleep]
