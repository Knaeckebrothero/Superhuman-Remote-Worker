"""Enumerate the parts of a session's stored config that are no longer usable.

Deliberately free of FastAPI and of policy decisions: callers hand in verdicts
already produced by the code that *enforces* them, so this module can never
drift from the enforcer. See docs/features/session_config_drift_resume.md.
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

#: Verdict reasons that an acknowledgment can resolve. ``workspace_tier`` is
#: absent on purpose — a lite-tier repository conflict is a config
#: incompatibility that keeps raising 400.
ACKNOWLEDGEABLE_REASONS = frozenset({"deleted", "revoked", "out_of_scope"})


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
        label = (
            DELETED_PROJECT_LABEL
            if verdict.reason == "deleted"
            else GENERIC_PROJECT_LABEL
        )
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


def drift_labels(items: list[DriftItem]) -> list[dict[str, Any]]:
    """Collapse items sharing a label into one row with a count.

    Revoked items all render the same generic string, so two of them would
    otherwise produce two identical lines. Every id is preserved, because the
    acknowledgment stays per-item.
    """
    grouped: dict[str, dict[str, Any]] = {}
    for item in items:
        row = grouped.get(item.label)
        if row is None:
            grouped[item.label] = {
                "label": item.label,
                "count": 1,
                "kind": item.kind,
                "reason": item.reason,
                "ids": [item.id],
            }
            continue
        row["count"] += 1
        row["ids"].append(item.id)
    return list(grouped.values())
