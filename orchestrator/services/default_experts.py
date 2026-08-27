"""Managed expert seeds and authoritative root-creation default selection.

The YAML bundles are bootstrap material only.  Once inserted, the DB row is the
runtime source of truth and startup never overwrites operator edits.  Both jobs
and sessions call :func:`resolve_root_expert`, which keeps precedence and grant
semantics out of individual HTTP/MCP/automation entry points.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from .grants_service import resolve_grants_for
from src.core.loader import canonical_config_name

ExpertType = Literal["worker", "session"]

BASE_CONFIG_NAMES: dict[ExpertType, str] = {
    "worker": "worker_base",
    "session": "session_base",
}

MANAGED_SEEDS: tuple[dict[str, Any], ...] = (
    {
        "managed_key": "application-default-worker-seed",
        "directory": "general-worker",
        "expert_type": "worker",
        "seed_version": 1,
    },
    {
        "managed_key": "application-default-session-seed",
        "directory": "assistant",
        "expert_type": "session",
        "seed_version": 1,
    },
)


class DefaultExpertUnavailable(RuntimeError):
    """A healthy root-creation path has no compatible application default."""


class ExpertSelectionError(ValueError):
    """An explicit/default expert is invisible, incompatible, or disallowed."""


@dataclass(frozen=True)
class ExpertSelection:
    expert: dict[str, Any]
    source: Literal["explicit", "project", "user", "application"]
    project_override: dict[str, Any] | None = None


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    return dict(value) if isinstance(value, dict) else {}


def load_seed_bundle(
    config_dir: Path, *, directory: str, expert_type: ExpertType
) -> dict[str, Any]:
    """Read one bundled expert as a raw DB overlay (never a merged snapshot)."""
    expert_dir = config_dir / "experts" / directory
    config_path = expert_dir / "config.yaml"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid managed expert config: {config_path}")
    extends = canonical_config_name(str(raw.pop("$extends", "worker_base")))
    expected = BASE_CONFIG_NAMES[expert_type]
    if extends != expected:
        raise ValueError(
            f"Managed expert {directory!r} extends {extends!r}; expected {expected!r}"
        )
    raw.pop("connections", None)

    prompts: dict[str, str] = {}
    for key, filename in (
        ("persona", "persona.txt"),
        ("instructions", "instructions.md"),
        ("strategic", "strategic.txt"),
        ("tactical", "tactical.txt"),
        ("summarization", "summarization_prompt.txt"),
    ):
        path = expert_dir / filename
        if path.exists():
            prompts[key] = path.read_text(encoding="utf-8")

    return {
        "name": directory,
        "display_name": str(
            raw.get("display_name") or directory.replace("-", " ").title()
        ),
        "description": str(raw.get("description") or "").strip(),
        "icon": str(raw.get("icon") or "smart_toy"),
        "color": str(raw.get("color") or "#6B7280"),
        "tags": list(raw.get("tags") or []),
        "expert_type": expert_type,
        "config": raw,
        "prompts": prompts,
    }


async def seed_managed_default_experts(db, config_dir: Path) -> dict[str, str]:
    """Insert missing managed experts and missing application pointers.

    Both operations are insert-only.  Existing expert content and an operator's
    current application pointer are preserved across restarts and upgrades.
    """
    seeded: dict[str, str] = {}
    for spec in MANAGED_SEEDS:
        bundle = load_seed_bundle(
            config_dir,
            directory=spec["directory"],
            expert_type=spec["expert_type"],
        )
        row, _created = await db.upsert_managed_expert(
            managed_key=spec["managed_key"],
            seed_version=spec["seed_version"],
            **bundle,
        )
        if row["expert_type"] != spec["expert_type"]:
            raise RuntimeError(
                f"Managed expert {spec['managed_key']} has incompatible type "
                f"{row['expert_type']}"
            )
        await db.ensure_application_expert_default(
            expert_type=spec["expert_type"], expert_id=str(row["id"])
        )
        seeded[spec["expert_type"]] = str(row["id"])
    return seeded


async def personal_defaults_allowed(
    db,
    *,
    user_id: str,
    project_ids: list[str] | None = None,
    is_admin: bool = False,
) -> bool:
    if is_admin:
        return True
    grants = await resolve_grants_for(
        db, user_id=user_id, project_ids=list(project_ids or [])
    )
    return bool(grants.get("personal_default_experts", True))


async def validate_explicit_expert(
    db,
    *,
    expert_id: str,
    expert_type: ExpertType,
    user_id: str,
    project_ids: list[str] | None = None,
    is_admin: bool = False,
) -> ExpertSelection:
    row = await db.get_expert_visible_by_id(
        expert_id,
        user_id=user_id,
        project_ids=list(project_ids or []),
        is_admin=is_admin,
    )
    if not row:
        raise ExpertSelectionError("Expert is unavailable")
    if row["expert_type"] != expert_type:
        raise ExpertSelectionError(
            f"A {expert_type} expert is required; selected expert is {row['expert_type']}"
        )
    return ExpertSelection(expert=row, source="explicit")


async def resolve_root_expert(
    db,
    *,
    expert_type: ExpertType,
    user_id: str,
    project_id: str | None = None,
    explicit_expert_id: str | None = None,
    is_admin: bool = False,
) -> ExpertSelection:
    """Resolve ``explicit > project > personal > application`` for a root."""
    project_ids = [project_id] if project_id else []
    if explicit_expert_id:
        explicit = await validate_explicit_expert(
            db,
            expert_id=explicit_expert_id,
            expert_type=expert_type,
            user_id=user_id,
            project_ids=project_ids,
            is_admin=is_admin,
        )
        project_override = None
        if project_id:
            link = await db.get_project_expert_link(
                project_id=project_id, expert_id=explicit_expert_id
            )
            if link:
                project_override = _json_object(link.get("config_override")) or None
        return ExpertSelection(
            expert=explicit.expert,
            source="explicit",
            project_override=project_override,
        )

    if project_id:
        project = await db.get_project_default_expert(
            project_id=project_id, expert_type=expert_type
        )
        if project:
            return ExpertSelection(
                expert=project,
                source="project",
                project_override=_json_object(project.get("config_override")) or None,
            )

    if await personal_defaults_allowed(
        db,
        user_id=user_id,
        project_ids=project_ids,
        is_admin=is_admin,
    ):
        personal = await db.get_user_expert_default(
            user_id=user_id, expert_type=expert_type
        )
        # The DB constraints guarantee type and the set endpoint guarantees
        # ownership.  Re-check ownership to fail closed if old/manual data exists.
        if personal and str(personal.get("owner_id")) == str(user_id):
            return ExpertSelection(expert=personal, source="user")

    application = await db.get_application_expert_default(expert_type)
    if application:
        return ExpertSelection(expert=application, source="application")
    raise DefaultExpertUnavailable(
        f"No application {expert_type} expert default is configured"
    )


__all__ = [
    "BASE_CONFIG_NAMES",
    "DefaultExpertUnavailable",
    "ExpertSelection",
    "ExpertSelectionError",
    "load_seed_bundle",
    "personal_defaults_allowed",
    "resolve_root_expert",
    "seed_managed_default_experts",
    "validate_explicit_expert",
]
