"""Account admission guard shared by HTTP and trusted application callers."""

from typing import Any, Mapping

from fastapi import HTTPException


def require_account_approved(user: Mapping[str, Any]) -> None:
    if not user.get("is_approved"):
        raise HTTPException(
            status_code=403,
            detail="Account pending approval. An administrator must approve your account.",
        )
