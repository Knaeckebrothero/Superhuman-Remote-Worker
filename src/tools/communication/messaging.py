"""Messaging tools for agent-human communication.

Provides the `send_message` tool that allows agents to send emails to
job owners (Phase 1) with messages stored as workspace files. Supports
async (fire-and-forget) and blocking (pause-until-reply) modes.

Messages are persisted in workspace/messages/<thread_id>/ as markdown
files with YAML frontmatter, making them visible via git_diff during
strategic review phases.
"""

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from langchain_core.tools import tool

from ..context import ToolContext

logger = logging.getLogger(__name__)


COMMUNICATION_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    "send_message": {
        "module": "communication.messaging",
        "function": "send_message",
        "description": (
            "Send a message to a human via email. Use mode='async' to continue "
            "working, or mode='blocking' to pause execution until a reply arrives. "
            "Use sparingly — the recipient receives this as an email."
        ),
        "category": "communication",
        "short_description": "Email the job owner. Supports async and blocking modes.",
        "phases": ["strategic", "tactical"],
    },
}


def _mask_email(email: str) -> str:
    """Mask an email address for display (a***@example.com)."""
    if not email or "@" not in email:
        return email
    local, domain = email.rsplit("@", 1)
    if len(local) <= 1:
        return f"*@{domain}"
    return f"{local[0]}***@{domain}"


def _build_message_content(
    direction: str,
    to: str,
    to_name: str,
    subject: str,
    thread_id: str,
    sequence: int,
    body: str,
    mode: str | None = None,
    status: str = "delivered",
    from_field: str = "agent",
) -> str:
    """Build a message file with YAML frontmatter."""
    frontmatter_lines = [
        "---",
        f"from: {from_field}",
        f"to: {to}",
        f"to_name: {to_name}",
        f"date: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"subject: {subject}",
        f"thread: {thread_id}",
        f"sequence: {sequence}",
    ]
    if mode:
        frontmatter_lines.append(f"mode: {mode}")
    frontmatter_lines.append(f"status: {status}")
    frontmatter_lines.append("---")
    frontmatter_lines.append("")

    return "\n".join(frontmatter_lines) + body + "\n"


def create_communication_tools(context: ToolContext) -> List[Any]:
    """Create communication tools with injected context.

    Args:
        context: ToolContext with workspace_manager and job_id

    Returns:
        List of LangChain tool functions
    """

    @tool
    async def send_message(
        to: str,
        subject: str,
        message: str,
        mode: str = "async",
        thread_id: Optional[str] = None,
    ) -> str:
        """Send a message to a human via email.

        Messages are stored as workspace files in messages/<thread_id>/ and
        delivered as emails through the orchestrator. Use sparingly — the
        recipient receives this as an email.

        Args:
            to: Recipient. Use "user" for the job owner.
            subject: Subject line (max 200 chars). Carried from first message
                when replying to an existing thread.
            message: Message body in markdown (max 5000 chars).
            mode: "async" to continue working, or "blocking" to pause
                execution until the recipient replies.
            thread_id: Reply to an existing thread. Omit to start a new thread.

        Returns:
            Confirmation with thread ID, delivery status, and file path.
        """
        # Validate inputs
        if not subject or not subject.strip():
            return "Error: subject is required."
        if len(subject) > 200:
            return f"Error: subject too long ({len(subject)} chars, max 200)."
        if not message or not message.strip():
            return "Error: message body is required."
        if len(message) > 5000:
            return f"Error: message too long ({len(message)} chars, max 5000)."
        if mode not in ("async", "blocking"):
            return f"Error: mode must be 'async' or 'blocking', got '{mode}'."

        # Check config restriction on recipients
        if to != "user":
            allowed = "project"
            if hasattr(context, "config") and context.config:
                comm_cfg = getattr(context.config, "communication", None)
                if isinstance(comm_cfg, dict):
                    allowed = comm_cfg.get("allowed_recipients", "project")
            if allowed == "owner":
                return (
                    "Error: this agent config restricts messaging to the job owner only "
                    "(to='user'). Set allowed_recipients: project in config to enable "
                    "multi-recipient messaging."
                )

        job_id = context.job_id
        if not job_id:
            return "Error: no job_id available."

        # Generate or validate thread_id
        is_new_thread = thread_id is None
        if is_new_thread:
            thread_id = uuid.uuid4().hex[:6]

        # Determine sequence number from workspace files
        msg_dir = f"messages/{thread_id}"
        sequence = 1
        if context.has_workspace():
            try:
                existing = context.workspace_manager.list_directory(msg_dir)
                sequence = len(existing) + 1
            except Exception:
                sequence = 1  # Directory doesn't exist yet

        # Build and write message file
        file_name = f"{sequence:03d}_sent.md"
        file_path = f"{msg_dir}/{file_name}"

        file_content = _build_message_content(
            direction="outbound",
            to="(resolved by orchestrator)",
            to_name="(resolved by orchestrator)",
            subject=subject,
            thread_id=thread_id,
            sequence=sequence,
            body=message,
            mode=mode,
            status="pending",
        )

        if context.has_workspace():
            try:
                context.workspace_manager.write_file(file_path, file_content)
            except Exception as e:
                return f"Error: failed to write message file: {e}"

            # Git commit
            if context.has_git():
                try:
                    context.workspace_manager.git_manager.commit(
                        f"Message sent: {subject[:50]}"
                    )
                except Exception:
                    pass  # Non-critical

        # Call orchestrator to send email + log + handle blocking
        orchestrator_url = os.getenv("ORCHESTRATOR_URL", "http://localhost:8085")
        api_url = f"{orchestrator_url}/api/jobs/{job_id}/messages/send"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    api_url,
                    json={
                        "to": to,
                        "subject": subject,
                        "message": message,
                        "mode": mode,
                        "thread_id": thread_id,
                        "project_id": getattr(context, "project_id", None),
                    },
                )

            if resp.status_code == 429:
                data = resp.json()
                return (
                    f"Rate limit exceeded. {data.get('error', '')} "
                    f"Retry after {data.get('retry_after_seconds', 0)} seconds. "
                    f"Message saved to workspace: {file_path}"
                )

            if resp.status_code != 200:
                error_detail = resp.text[:200]
                logger.warning(
                    f"Orchestrator message send failed ({resp.status_code}): {error_detail}"
                )
                return (
                    f"Message saved to workspace ({file_path}) but email delivery "
                    f"failed: HTTP {resp.status_code}. The message is preserved in "
                    f"the workspace and can be retried."
                )

            data = resp.json()
            masked_recipient = data.get("recipient", "unknown")

            # Update message file status to delivered
            if context.has_workspace():
                try:
                    updated_content = file_content.replace(
                        "status: pending", "status: delivered"
                    )
                    if "to_name" in data:
                        updated_content = updated_content.replace(
                            "to_name: (resolved by orchestrator)",
                            f"to_name: {data['to_name']}",
                        )
                    updated_content = updated_content.replace(
                        "to: (resolved by orchestrator)",
                        f"to: {masked_recipient}",
                    )
                    context.workspace_manager.write_file(file_path, updated_content)
                except Exception:
                    pass  # Non-critical

        except httpx.RequestError as e:
            logger.warning(f"Failed to reach orchestrator for message send: {e}")
            return (
                f"Message saved to workspace ({file_path}) but orchestrator "
                f"unreachable: {e}. Email delivery skipped."
            )

        # If blocking mode, request job freeze
        if mode == "blocking" and resp.status_code == 200:
            context.request_freeze(
                {
                    "status": "waiting_for_reply",
                    "freeze_type": "blocking_message",
                    "thread_id": thread_id,
                    "subject": subject,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "job_id": job_id,
                }
            )

            return (
                f"Message sent to {masked_recipient} (thread: {thread_id}).\n"
                f"File: {file_path}\n\n"
                f"Job execution is now paused. Waiting for reply. "
                f"Execution will resume automatically when the recipient responds."
            )

        # Async mode confirmation
        return (
            f"Message sent to {masked_recipient} (thread: {thread_id}).\n"
            f"File: {file_path}\n"
            f"Mode: async — continuing execution."
        )

    return [send_message]
