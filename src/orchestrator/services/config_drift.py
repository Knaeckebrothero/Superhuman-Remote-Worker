"""Enumerate the parts of a session's stored config that are no longer usable.

Deliberately free of FastAPI and of policy decisions: callers hand in verdicts
already produced by the code that *enforces* them, so this module can never
drift from the enforcer. See knowledge-history/done/session_config_drift_resume.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


#: Revoked and out-of-scope items are described without naming them: they still
#: exist and belong to someone, so naming them would confirm their existence and
#: current name. Deleted rows carry no such risk.
GENERIC_CONNECTOR_LABEL = "a connector you no longer have access to"
GENERIC_PROJECT_LABEL = "a project you no longer have access to"
DELETED_PROJECT_LABEL = "a project that no longer exists"
#: Archived is not a permission loss and the caller is still a member, so this
#: one names the state plainly instead of using the generic label.
ARCHIVED_PROJECT_LABEL = "a project that has been archived"

#: Verdict reasons that an acknowledgment can resolve. ``workspace_tier`` is
#: absent on purpose — a lite-tier repository conflict is a config
#: incompatibility that keeps raising 400. ``archived`` IS acknowledgeable:
#: a project archived under a live session is a lifecycle change the owner can
#: accept and continue without, whereas leaving it out of this set would route
#: it through :func:`blocking_denials` and refuse the resume as *corrupt*.
ACKNOWLEDGEABLE_REASONS = frozenset({"deleted", "revoked", "out_of_scope", "archived"})


@dataclass(frozen=True)
class DriftItem:
    """One unusable configuration element.

    ``id`` is the stable acknowledgment key and is namespaced by kind so the
    three families cannot collide.
    """

    id: str
    kind: str
    reason: str
    label: str


async def collect_config_drift(
    db,
    thread: dict[str, Any],
    *,
    owner: dict[str, Any],
    project_ids: list[Any],
    datasource_ids: list[Any],
    grant_violations: list[str],
    tombstones: dict[str, str] | None = None,
) -> list[DriftItem]:
    """Every acknowledgeable drift item for one thread, in a stable order:
    connectors, then projects, then grants.

    ``project_ids`` and ``datasource_ids`` are verdict objects, not raw ids —
    they come from ``_classify_thread_project_ids`` and
    ``classify_datasource_selection`` respectively. ``grant_violations`` are the
    strings ``evaluate()`` produced, carried out of the resolve status.
    """
    names = tombstones or {}
    items: list[DriftItem] = []

    for verdict in datasource_ids:
        if not verdict.denied or verdict.reason not in ACKNOWLEDGEABLE_REASONS:
            continue
        if verdict.reason == "deleted":
            label = names.get(verdict.datasource_id, verdict.datasource_id)
        else:
            label = GENERIC_CONNECTOR_LABEL
        items.append(
            DriftItem(
                id=f"connector:{verdict.datasource_id}",
                kind="connector",
                reason=verdict.reason,
                label=label,
            )
        )

    for verdict in project_ids:
        if not verdict.denied or verdict.reason not in ACKNOWLEDGEABLE_REASONS:
            continue
        label = {
            "deleted": DELETED_PROJECT_LABEL,
            "archived": ARCHIVED_PROJECT_LABEL,
        }.get(verdict.reason, GENERIC_PROJECT_LABEL)
        items.append(
            DriftItem(
                id=f"project:{verdict.project_id}",
                kind="project",
                reason=verdict.reason,
                label=label,
            )
        )

    for violation in grant_violations or []:
        key, _, message = violation.partition(": ")
        items.append(
            DriftItem(
                id=f"grant:{key}",
                kind="grant",
                reason="revoked",
                label=message or key,
            )
        )

    return items


def blocking_denials(
    datasource_verdicts: list[Any], project_verdicts: list[Any]
) -> list[str]:
    """Denials no acknowledgment can clear — corruption and tier conflicts.

    These must refuse at resume rather than fall through to a 200: they deny
    at attach regardless, and a session that resumes and then cannot attach
    is the silent hang this feature exists to remove.
    """
    blocking: list[str] = []
    for verdict in datasource_verdicts:
        if verdict.denied and verdict.reason not in ACKNOWLEDGEABLE_REASONS:
            blocking.append(f"connector:{verdict.datasource_id}")
    for verdict in project_verdicts:
        if verdict.denied and verdict.reason not in ACKNOWLEDGEABLE_REASONS:
            blocking.append(f"project:{verdict.project_id}")
    return blocking


def acknowledged_drift_ids(metadata: Any) -> set[str]:
    """Drift ids the owner already accepted losing.

    Accepts a raw JSON string as well as a dict: asyncpg returns JSONB columns
    as strings, and an ``isinstance(x, dict)`` guard without a parse silently
    turns this feature off.
    """
    if isinstance(metadata, str):
        import json

        try:
            metadata = json.loads(metadata)
        except (ValueError, TypeError):
            return set()
    if not isinstance(metadata, dict):
        return set()
    ack = metadata.get("config_drift_ack") or {}
    if not isinstance(ack, dict):
        return set()
    return {str(key) for key in ack}


def strip_acknowledged(ids: list[str], ack: set[str], *, prefix: str) -> list[str]:
    """Drop ids whose namespaced drift key was acknowledged."""
    return [value for value in ids if f"{prefix}:{value}" not in ack]


def acknowledged_grant_keys(metadata: Any) -> set[str]:
    """Acknowledged grant keys, unprefixed, ready to compare against the keys
    ``evaluate()`` puts in front of each violation string."""
    return {
        key[len("grant:") :]
        for key in acknowledged_drift_ids(metadata)
        if key.startswith("grant:")
    }
