"""Server-owned metadata identifying a project's native knowledge base."""

from typing import Any


# A marked ``kb`` datasource is a management surface over the project's own
# knowledge base. Its notes are indexed under the project id, not a second time
# under the datasource UUID. Admission, grants and indexing policy belong to
# the callers; interpreting this marker does not authorize a client to set it.
NATIVE_PROJECT_CONFIG_KEY = "native_project_id"


def native_kb_project_id(datasource: dict[str, Any] | None) -> str | None:
    """Read a native project's marker without validating or normalizing its id."""
    if not datasource:
        return None
    config = datasource.get("config") or {}
    if not isinstance(config, dict):
        return None
    value = config.get(NATIVE_PROJECT_CONFIG_KEY)
    return str(value) if value else None
