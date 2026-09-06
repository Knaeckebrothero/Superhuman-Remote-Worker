"""Configuration write validation and override merge semantics.

These are the existing HTTP-compatible helpers, independent of application
startup. Merge copies only traversed mappings; None deletes a key and a list
replaces its predecessor. Do not substitute the loader's deep-copy merge.
"""

from typing import Any

from fastapi import HTTPException

from orchestrator.services.agent_pod_entrypoint import (
    InvalidConfigNameError,
    validate_config_name,
)


def deep_merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep merge two dicts. Override wins for scalars/lists; dicts merge recursively."""
    result = base.copy()
    for key, value in override.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


def validated_config_name(config_name: str | None) -> str | None:
    """Return a caller-supplied ``config_name``, or 422 naming the rule it broke.

    ``config_name`` is the one caller-controlled word in the agent pod's
    ``sh -c`` entrypoint. Both provisioners re-check it at their own boundary
    (``services/agent_pod_entrypoint.validate_config_name``, security audit
    2026-08-27 finding #3), but they are reached from fire-and-forget tasks and
    from rows read back long after the request that wrote them — a hostile
    value that gets *persisted* explodes on every later resume, recycle and
    magic-link wake, with no request left to answer. So the allow-list also
    runs here, on WRITE, exactly like ``_with_validated_tool_overrides``: the
    caller gets one clean 422 and the row is never created.

    Deliberately NOT applied to values read back out of the database. A row
    poisoned before this guard existed must still be listable, resumable-to-a
    -clear-failure and deletable; it fails loudly at its provisioning attempt
    instead (see the fire-and-forget handlers in this module).
    """
    try:
        return validate_config_name(config_name)
    except InvalidConfigNameError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
