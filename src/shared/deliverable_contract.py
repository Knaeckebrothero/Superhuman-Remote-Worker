"""Canonical deliverable-contract vocabulary shared by runtime and server.

The list is caller-authored, but its normalized value becomes immutable at
job admission.  Keep parsing here so the task brief and the server gate cannot
disagree about identity.
"""

from __future__ import annotations

import json
import re
from typing import Any

KB_DELIVERABLE_PREFIX = "kb:"
PR_DELIVERABLE_PREFIX = "pr:"
CLONED_REPO_PREFIX = "repos/"

_REPOSITORY_PART = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})$")


class DeliverableContractError(ValueError):
    """A manifest entry has no unambiguous canonical meaning."""


def normalize_repository_identity(value: Any) -> str | None:
    """Return canonical ``owner/repository`` identity, or ``None``.

    Forge repository paths are case-insensitive on every currently supported
    provider.  Case-folding gives one stable identity for contracts, persisted
    PR records, connector URLs, retries, and database uniqueness.
    """

    if not isinstance(value, str):
        return None
    candidate = value.strip().strip("/")
    parts = candidate.split("/")
    if len(parts) != 2 or not all(_REPOSITORY_PART.fullmatch(part) for part in parts):
        return None
    return "/".join(part.casefold() for part in parts)


def normalize_deliverable(value: Any) -> str | None:
    """Normalize one path, knowledge note, or pull-request deliverable."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    lowered = candidate.casefold()
    if lowered.startswith(KB_DELIVERABLE_PREFIX):
        slug = candidate[len(KB_DELIVERABLE_PREFIX) :].strip()
        return f"{KB_DELIVERABLE_PREFIX}{slug}" if slug else None
    if lowered.startswith(PR_DELIVERABLE_PREFIX):
        repository = normalize_repository_identity(
            candidate[len(PR_DELIVERABLE_PREFIX) :]
        )
        return f"{PR_DELIVERABLE_PREFIX}{repository}" if repository else None
    # Leading ``./`` and ``/`` are presentation variants, not different
    # workspace authorities.  Strip them before deciding whether the path is
    # in the job tree (``repo/``) or an attached clone (``repos/``).  Never
    # let traversal or separator ambiguity normalize into the other domain.
    while candidate.startswith("./"):
        candidate = candidate[2:]
    if candidate.startswith("//"):
        return None
    if candidate.startswith("/"):
        candidate = candidate[1:]
    if not candidate or "\\" in candidate or "\x00" in candidate:
        return None

    job_tree_prefixed = candidate.startswith("repo/")
    if job_tree_prefixed:
        candidate = candidate[len("repo/") :]
        # ``repo/repos/...`` is ambiguous with the platform-owned attached
        # clone root.  Refuse it rather than silently changing authority.
        if candidate.startswith(CLONED_REPO_PREFIX):
            return None

    parts = candidate.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    return candidate.strip() or None


def parse_required_deliverables(source: Any, *, strict: bool = False) -> list[str]:
    """Extract an order-preserving, deduplicated canonical manifest.

    ``strict`` is used at admission, where silently dropping an invalid entry
    would weaken the caller's contract.  Restore/presentation paths remain
    tolerant for historical data.
    """

    value = source
    if isinstance(source, str):
        try:
            decoded = json.loads(source)
        except (json.JSONDecodeError, TypeError, ValueError):
            decoded = None
        if isinstance(decoded, dict):
            value = decoded.get("required_deliverables")
        elif isinstance(decoded, (list, tuple)):
            value = decoded
        else:
            # At this boundary a bare string historically denotes serialized
            # context, not one deliverable. Admission receives a list and
            # therefore does not need an ambiguous single-string shortcut.
            value = []
    elif isinstance(source, dict):
        value = source.get("required_deliverables")
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        if strict and value is not None:
            raise DeliverableContractError("required_deliverables must be a list")
        return []

    normalized: list[str] = []
    for entry in value:
        item = normalize_deliverable(entry)
        if item is None:
            if strict:
                raise DeliverableContractError(
                    f"invalid deliverable contract entry: {entry!r}"
                )
            continue
        if item not in normalized:
            normalized.append(item)
    return normalized


def pr_repositories(deliverables: Any) -> list[str]:
    """Canonical repositories named by PR deliverables."""

    return [
        item[len(PR_DELIVERABLE_PREFIX) :]
        for item in parse_required_deliverables(deliverables)
        if item.startswith(PR_DELIVERABLE_PREFIX)
    ]


def is_cloned_repo_deliverable(value: Any) -> bool:
    """Whether an entry names content inside an unversioned clone."""

    candidate = normalize_deliverable(value)
    if candidate is None:
        return False
    if not candidate.startswith(CLONED_REPO_PREFIX):
        return False
    return bool(candidate[len(CLONED_REPO_PREFIX) :].strip("/"))


def cloned_repo_deliverables(deliverables: Any) -> list[str]:
    """Canonical unversioned clone paths, preserving declared order."""

    return [
        item
        for item in parse_required_deliverables(deliverables)
        if item.startswith(CLONED_REPO_PREFIX) and bool(item[len(CLONED_REPO_PREFIX) :])
    ]


def cloned_repo_alias(value: str) -> str | None:
    """First path component after ``repos/``, without treating it as authority."""

    candidate = normalize_deliverable(value)
    if candidate is None or not is_cloned_repo_deliverable(candidate):
        return None
    return candidate[len(CLONED_REPO_PREFIX) :].split("/", 1)[0] or None


__all__ = [
    "CLONED_REPO_PREFIX",
    "DeliverableContractError",
    "KB_DELIVERABLE_PREFIX",
    "PR_DELIVERABLE_PREFIX",
    "cloned_repo_alias",
    "cloned_repo_deliverables",
    "is_cloned_repo_deliverable",
    "normalize_deliverable",
    "normalize_repository_identity",
    "parse_required_deliverables",
    "pr_repositories",
]
