"""Agent-initiated workspace-tier-upgrade request tool.

A lite (``virtual``/``none``) agent has no shell to "attempt", so it can never
trip the sudo→VM freeze a sandbox agent uses to ask for more privilege. This
gives a lite agent an explicit, auditable request path:
``request_workspace_upgrade(reason)`` sets a ``workspace_upgrade_required``
freeze — it only REQUESTS, it never flips the tier. The transport turns that
freeze into a human-in-the-loop offer (``workspace_upgrade.needed``) the user
must approve before anything provisions (workspace_tier_upgrade.md §4.2 S5,
§4.4 Sec-4: the tier-control surface stays out of the agent's reach).

Category ``core`` (not an execution category), so it survives
``filter_tools_by_backend`` on the lite tiers where it actually matters; the
session only exposes it while the backend has no shell.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from langchain_core.tools import tool

from ..context import ToolContext

logger = logging.getLogger(__name__)


WORKSPACE_UPGRADE_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    "request_workspace_upgrade": {
        "module": "core.upgrade",
        "function": "request_workspace_upgrade",
        "description": (
            "Request an upgrade from the lite workspace to a real sandbox "
            "container (shell, git, file tools). A human approves before "
            "anything is provisioned; you only request."
        ),
        "category": "core",
        "short_description": "Ask to upgrade to a real sandbox workspace.",
        "phases": ["strategic", "tactical"],  # Available in both modes
    },
}


def create_workspace_upgrade_tools(context: ToolContext) -> List[Any]:
    """Create the agent-initiated workspace-upgrade request tool.

    No workspace/todo dependency — it only records a freeze request on the
    ToolContext, so it loads on the lite tiers (``todo_manager=None``).
    """

    @tool
    async def request_workspace_upgrade(reason: str) -> str:
        """Request an upgrade from the lite (virtual) workspace to a real sandbox.

        Call this when the task needs capabilities the lite workspace lacks — a
        shell, git, running code or builds, or browser control. You are only
        REQUESTING: a human is shown your request and must approve it before any
        workspace is provisioned. You do not need to take any further action; if
        approved, a sandbox is provisioned and shell/git/file tools become
        available on a later turn, and your existing files carry over.

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
            "Recorded a request to upgrade to a sandbox workspace. A human will "
            "review and approve it — you don't need to do anything else now. If "
            "approved, shell, git, and file tools will become available shortly "
            "and your existing files will carry over."
        )

    return [request_workspace_upgrade]
