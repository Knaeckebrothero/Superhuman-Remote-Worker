"""Internal helpers shared by framework-independent job handlers."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .. import formatters as fmt
from ..client import AsyncCockpitClient, MutationOutcomeUnknown
from .descriptors import CallerCtx

_TRANSPORT_DENY = frozenset({"api_key", "base_url", "env_keys"})
_TRANSPORT_DENY_SUFFIX = "_api_key"


def transport_key_paths(value: Any, prefix: str = "") -> list[str]:
    """Return forbidden credential/transport paths in a config override."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            normalized = str(key).lower()
            if normalized in _TRANSPORT_DENY or normalized.endswith(
                _TRANSPORT_DENY_SUFFIX
            ):
                found.append(path)
            else:
                found.extend(transport_key_paths(nested, path))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            found.extend(transport_key_paths(nested, f"{prefix}[{index}]"))
    return found


def clean_job_id(job_id: str) -> str:
    cleaned = str(job_id or "").strip()
    while cleaned.endswith("...") or cleaned.endswith("\u2026"):
        cleaned = cleaned[:-3] if cleaned.endswith("...") else cleaned[:-1]
        cleaned = cleaned.strip()
    return cleaned


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
        return True
    except (TypeError, ValueError):
        return False


async def resolve_job_id(
    client: AsyncCockpitClient,
    caller: CallerCtx,
    job_id: str,
) -> str:
    """Resolve visible UUID prefixes only for adapters that historically did."""
    if not caller.resolve_job_id_prefixes:
        # MCP's pre-unification wrappers forwarded the supplied ID verbatim.
        # Prefix/ellipsis cleanup is an agent/session compatibility feature,
        # not a silent change to the existing MCP request contract.
        return str(job_id)

    cleaned = clean_job_id(job_id)
    if not cleaned or _is_uuid(cleaned) or len(cleaned) < 8:
        return cleaned

    jobs = await client.list_jobs(limit=500)
    matches = [
        str(job.get("id"))
        for job in jobs
        if job.get("id") and str(job.get("id")).startswith(cleaned)
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        sample = ", ".join(matches[:5])
        raise ValueError(
            f"Job ID prefix '{cleaned}' is ambiguous; matches include: {sample}"
        )
    return cleaned


def format_action_error(action: str, target: str, error: Exception) -> str:
    """Keep an ambiguous mutation distinct from a confirmed failure."""
    if isinstance(error, MutationOutcomeUnknown):
        return f"Action '{action}' has an unknown outcome for {target}:\n{error}"
    return fmt.format_action_error(action, target, error)


def short_id(value: Any) -> str:
    text = str(value or "")
    return text[:8] if text else "unknown"
