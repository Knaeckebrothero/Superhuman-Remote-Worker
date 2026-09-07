"""Revalidate the current creator for trusted application job submission.

Only application composition supplies the creator ID, for example from a stored
benchmark run. This is not a transport authentication method: it accepts neither
credentials nor requests, and must not be exposed as an HTTP identity shortcut.
"""

from typing import Any, Protocol

from fastapi import HTTPException

from orchestrator.security.account_approval import require_account_approved


class JobCreatorStore(Protocol):
    async def get_user(self, user_id: str) -> dict[str, Any] | None: ...


async def authenticate_job_creator(
    user_id: str, store: JobCreatorStore
) -> tuple[dict[str, Any], None]:
    user = await store.get_user(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    # Retain the principal shape of the old MCP-header benchmark bridge.
    # The stored creator has no request-scoped token or admin-shadow header.
    user["auth_method"] = "mcp"
    user["scopes"] = []
    require_account_approved(user)
    return {**user, "real_is_admin": bool(user.get("is_admin"))}, None
