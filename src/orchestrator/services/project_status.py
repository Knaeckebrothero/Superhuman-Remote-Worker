"""Pure archived-project classification and its existing refusal detail."""

from typing import Any


#: Refusal body. A bare sentence, not a ``{code, message}`` dict: house style
#: is overwhelmingly plain-string ``detail`` and the cockpit's generic error
#: path types it as a string (a dict renders as ``[object Object]``).
PROJECT_ARCHIVED_DETAIL = (
    "This project is archived. Unarchive it before creating new work."
)


def project_is_archived(project: Any) -> bool:
    """Whether ``project`` (a row dict) is in the archived lifecycle state.

    Case-insensitive, and deliberately narrow: only the literal ``archived``
    counts. A NULL, empty or unrecognised status is treated as live, so a row
    nobody can classify keeps working rather than silently becoming read-only.
    """
    if not isinstance(project, dict):
        return False
    return str(project.get("status") or "").strip().lower() == "archived"
