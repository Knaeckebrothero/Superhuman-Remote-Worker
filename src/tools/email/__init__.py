"""Email toolkit — IMAP/SMTP mailbox operations.

Provides email tools when an ``email`` datasource is attached to a job:
- List folders / list / search / read messages (read tier)
- Move and flag messages (read_write tier)
- Compose drafts into the Drafts folder (draft tier)
- Send mail via SMTP (send tier, gated)

The connection wrapper lives in ``connection.EmailConnection``; see
knowledge-base/knowledge/features/email_datasource.md for the datasource design.
"""

from typing import Any, Dict, List

from ..context import ToolContext


def create_email_tools(context: ToolContext) -> List[Any]:
    """Create all email tools with injected context.

    Args:
        context: ToolContext with an email datasource (EmailConnection)

    Returns:
        List of LangChain tool functions

    Raises:
        ValueError: If the email datasource is not available in context
    """
    from .tools import create_email_tools as _impl

    return _impl(context)


def get_email_metadata() -> Dict[str, Dict[str, Any]]:
    """Get metadata for all email tools."""
    from .tools import EMAIL_TOOLS_METADATA

    return EMAIL_TOOLS_METADATA
