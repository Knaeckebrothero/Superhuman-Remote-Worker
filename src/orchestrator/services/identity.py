"""The public projection of an authenticated user.

One shape, one owner. Every API response that hands a caller "who am I"
goes through :func:`user_dict`, so a field cannot be added to the auth
bootstrap and silently missed by another identity surface.
"""

from typing import Any


def user_dict(user: dict[str, Any]) -> dict[str, Any]:
    """Build the public user dict for API responses."""
    return {
        "id": str(user["id"]),
        "display_name": user["display_name"],
        "avatar_color": user["avatar_color"],
        "email": user.get("email"),
        "default_project_id": str(user["default_project_id"])
        if user.get("default_project_id")
        else None,
        "is_admin": user.get("is_admin", False),
        "is_approved": user.get("is_approved", False),
        "can_use_vm": bool(user.get("can_use_vm", False)),
        "created_at": user["created_at"],
    }
