"""Agent-initiated workspace-tier-upgrade request tool.

A lite (``virtual``/``none``) agent has no shell to "attempt", so it can never
trip the sudo→VM freeze a sandbox agent uses to ask for more privilege. This
gives a lite agent an explicit, auditable request path:
``request_workspace_upgrade(reason)`` sets a ``workspace_upgrade_required``
freeze — it only REQUESTS, it never flips the tier. The transport turns that
freeze into a human-in-the-loop offer (``workspace_upgrade.needed``) the user
must approve before anything provisions (workspace_tier_upgrade.md §4.2 S5,
§4.4 Sec-4: the tier-control surface stays out of the agent's reach).

"Freeze" is a misnomer on the session path: ``request_freeze`` only sets a
one-shot slot the graph reads and clears mid-loop, then falls through to the
next LLM iteration (persistent_graph.py, "Continue the inner loop"). Sessions
have no should_stop/freeze_data state — the agent keeps talking and ends its
turn normally, and nothing resumes it. That is why the copy below refuses to
promise a continuation: only the human can grant one.

Category ``core`` (not an execution category), so it survives
``filter_tools_by_backend`` on the lite tiers where it actually matters; the
session only exposes it while the backend has no shell.
"""

import logging
from datetime import datetime, timezone
from typing import Any, List

from langchain_core.tools import tool

from agent.tools.context import ToolContext

from shared.tool_catalog.definitions import (
    WORKSPACE_UPGRADE_TOOLS_METADATA as WORKSPACE_UPGRADE_TOOLS_METADATA,
)

logger = logging.getLogger(__name__)


def create_workspace_upgrade_tools(context: ToolContext) -> List[Any]:
    """Create the agent-initiated workspace-upgrade request tool.

    No workspace/todo dependency — it only records a freeze request on the
    ToolContext, so it loads on the lite tiers (``todo_manager=None``).
    """

    @tool
    async def request_workspace_upgrade(reason: str) -> str:
        """Request an upgrade from the lite (virtual) workspace to a real sandbox.

        Call this when the task needs capabilities the lite workspace lacks — a
        shell, git, running code or builds, or browser control.

        You are only REQUESTING. A human is shown your request and decides. If
        they approve, a sandbox is provisioned and your existing files carry
        over; they may then ask you to continue, or simply pick the conversation
        back up themselves. If they decline, they will tell you why.

        This request does not provision a workspace or automatically resume
        the task. Explain what the upgrade enables, tell the user to send a
        follow-up message after it completes, and continue useful preparation
        with the tools currently available. Check the new tools when the user
        returns; approval alone is not proof that provisioning succeeded.

        Args:
            reason: A short, concrete explanation of why a real workspace is
                needed (e.g. "need to run pytest", "clone and build the repo").

        Returns:
            Confirmation that the request was recorded.
        """
        context.request_freeze(
            {
                "freeze_type": "workspace_upgrade_required",
                "target_tier": "sandbox",
                "reason": reason or "The task needs a real workspace (shell/git).",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
        logger.info("request_workspace_upgrade requested: reason=%r", reason)
        return (
            "Recorded your request for a sandbox workspace — a human will see "
            "it and decide. The request has not started a workspace or added "
            "tools. Explain what it enables and continue useful preparation. "
            "After an approved upgrade completes, the user can send a "
            "follow-up message to continue the task; check the newly available "
            "tools then. Existing files carry over during the upgrade."
        )

    return [request_workspace_upgrade]
