"""Resolution helpers for the Agent Skills runtime (Slice 2).

Kept separate from main.py / loader.py so the menu-precedence and workspace-
mapping logic is small and unit-testable in isolation (mirrors
``expert_resolution.py``). No DB or framework imports live here; the disk reads
are the explicit bundled system-skill floors used by persistent sessions.

Design: docs/features/agent_skills.md (Slice 2).
Managed product guide: docs/features/app_guide_skill.md (M1).
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.core.expert_resolution import expert_precedence_key

logger = logging.getLogger(__name__)

APP_GUIDE_SKILL = "app-guide"
APP_GUIDE_LOADER_TOOL = "read_product_guide"
APP_GUIDE_BREAK_GLASS_ENV = "APP_GUIDE_BREAK_GLASS_DISABLED"
PRESENT_WITH_CANVAS_SKILL = "present-with-canvas"
RESERVED_SYSTEM_SKILL_NAMES = frozenset({APP_GUIDE_SKILL})
_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


def app_guide_break_glass_disabled(
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return the operator-owned emergency-disable policy.

    This is deliberately a negative, default-off switch rather than an
    ordinary user/deployment capability flag. All delivery seams call this
    helper so catalog installation, reader instantiation, and health reporting
    cannot interpret the environment differently.
    """

    env = os.environ if environ is None else environ
    return (
        str(env.get(APP_GUIDE_BREAK_GLASS_ENV, "")).strip().lower()
        in _TRUTHY_ENV_VALUES
    )


def is_reserved_system_skill_name(name: str) -> bool:
    """Return whether ``name`` is owned by the running SRW product.

    Reserved product artifacts cannot be created/imported as ordinary DB skills
    and are replaced from the running bundle at the persistent-session boundary.
    """

    return name in RESERVED_SYSTEM_SKILL_NAMES


def skill_bundle_digest(files: dict[str, str]) -> str:
    """Return a deterministic digest for a validated text skill bundle."""

    canonical = json.dumps(
        files,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _read_bundled_skill(
    skill_name: str, *, skills_root: Path | None = None
) -> tuple[dict[str, Any], dict[str, str]]:
    """Read and validate one bundled text skill from the running checkout."""

    from src.core.skill_format import (
        parse_skill_md,
        skill_identity,
        validate_skill_path,
    )

    root = skills_root or Path(__file__).resolve().parents[2] / "config" / "skills"
    skill_dir = root / skill_name
    bundled_files: dict[str, str] = {}
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file():
            continue
        rel_path = path.relative_to(skill_dir).as_posix()
        validate_skill_path(rel_path)
        bundled_files[rel_path] = path.read_text(encoding="utf-8")
    frontmatter, _ = parse_skill_md(bundled_files["SKILL.md"])
    name, _description = skill_identity(frontmatter)
    if name != skill_name:
        raise ValueError(f"Bundled skill name must be {skill_name!r} (got {name!r})")
    return frontmatter, bundled_files


def app_guide_health_snapshot(
    *,
    skills_root: Path | None = None,
    reader_available: bool = True,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the bounded managed-guide delivery state for health surfaces.

    No exception text or filesystem path is returned. Readiness for the chat
    runtime is intentionally outside this snapshot.
    """

    if app_guide_break_glass_disabled(environ):
        return {"state": "disabled", "reason": "operator_break_glass"}

    try:
        _frontmatter, bundled_files = _read_bundled_skill(
            APP_GUIDE_SKILL,
            skills_root=skills_root,
        )
        # Exercise the same deterministic digest path the reader verifies.
        skill_bundle_digest(bundled_files)
    except (KeyError, OSError):
        return {"state": "unavailable", "reason": "bundle_unavailable"}
    except (UnicodeDecodeError, ValueError):
        return {"state": "unavailable", "reason": "bundle_invalid"}

    if not reader_available:
        return {"state": "unavailable", "reason": "reader_unavailable"}
    return {"state": "ready"}


def _normalize_catalog(skills: dict[str, Any]) -> tuple[dict[str, Any], list, dict]:
    """Deep-copy a catalog and ensure its menu/files containers have safe types."""

    catalog = copy.deepcopy(skills or {})
    menu = catalog.get("menu")
    files = catalog.get("files")
    if not isinstance(menu, list):
        menu = []
        catalog["menu"] = menu
    if not isinstance(files, dict):
        files = {}
        catalog["files"] = files
    return catalog, menu, files


def _remove_skill(catalog: dict[str, Any], name: str) -> None:
    """Remove one skill from both sides of a catalog when present."""

    menu = catalog.get("menu")
    if isinstance(menu, list):
        catalog["menu"] = [
            item
            for item in menu
            if not isinstance(item, dict) or item.get("name") != name
        ]
    files = catalog.get("files")
    if isinstance(files, dict):
        files.pop(name, None)


def resolve_skill_menu(
    rows: list[dict[str, Any]], user_id: str, project_ids: set[str]
) -> list[dict[str, Any]]:
    """Dedup skills by name keeping the highest-precedence row, then sort by name.

    Unlike experts (``pick_expert_by_name``), the menu keeps ALL names and does
    NOT drop tier-0 rows — **bundled is the floor**. A higher-precedence row
    (owner > project > global) with the same ``name`` shadows the bundled one
    entirely (replacement). Order is by ``name`` for a deterministic menu.
    """
    best: dict[str, tuple[tuple, dict]] = {}
    for row in rows:
        key = expert_precedence_key(row, user_id, project_ids)
        cur = best.get(row["name"])
        if cur is None or key > cur[0]:
            best[row["name"]] = (key, row)
    return [row for _key, row in sorted(best.values(), key=lambda kr: kr[1]["name"])]


def skill_files_to_workspace(
    skills_files: dict[str, dict[str, str]],
) -> dict[str, str]:
    """Map ``{skill_name: {rel_path: content}}`` to workspace paths rooted at
    ``skills/<skill_name>/<rel_path>`` (the layout ``use_skill`` reads from)."""
    out: dict[str, str] = {}
    for name, files in skills_files.items():
        for rel_path, content in files.items():
            out[f"skills/{name}/{rel_path}"] = content
    return out


def add_default_canvas_skill(
    skills: dict[str, Any], *, skills_root: Path | None = None
) -> dict[str, Any]:
    """Add only the bundled Canvas companion skill as a catalog floor.

    DB-backed skills are an optional feature, while Canvas is a default
    persistent-session capability.  This narrow floor keeps the Canvas usage
    guidance available when the skills DB (or the entire resolved-config path)
    is disabled without implicitly enabling every unrelated bundled skill.

    A resolved user/global skill with the same name still wins: if either its
    menu entry or files are already present, this helper does not mix bundled
    content into that replacement.  Final capability scoping remains the job
    of :func:`scope_skills_for_tools` after tools actually instantiate.
    """

    catalog, menu, files = _normalize_catalog(skills)

    if (
        any(
            isinstance(item, dict) and item.get("name") == PRESENT_WITH_CANVAS_SKILL
            for item in menu
        )
        or PRESENT_WITH_CANVAS_SKILL in files
    ):
        return catalog

    try:
        frontmatter, bundled_files = _read_bundled_skill(
            PRESENT_WITH_CANVAS_SKILL,
            skills_root=skills_root,
        )
    except (KeyError, OSError, UnicodeDecodeError, ValueError) as exc:
        logger.warning("Bundled Canvas companion skill is unavailable: %s", exc)
        return catalog

    description = str(frontmatter.get("description", "") or "").strip()
    menu.append(
        {
            "id": PRESENT_WITH_CANVAS_SKILL,
            "name": PRESENT_WITH_CANVAS_SKILL,
            "display_name": frontmatter.get(
                "display_name", PRESENT_WITH_CANVAS_SKILL.replace("-", " ").title()
            ),
            "description": description,
            "icon": frontmatter.get("icon", "extension"),
            "color": frontmatter.get("color", "#6B7280"),
            "tags": frontmatter.get("tags", []),
        }
    )
    menu.sort(
        key=lambda item: str(item.get("name", "")) if isinstance(item, dict) else ""
    )
    files[PRESENT_WITH_CANVAS_SKILL] = bundled_files
    return catalog


def add_persistent_system_skills(
    skills: dict[str, Any], *, skills_root: Path | None = None
) -> dict[str, Any]:
    """Install the running product's managed skills into a session catalog.

    ``app-guide`` is deliberately unlike an ordinary skill:

    - any frozen/user/global entry with its reserved name is removed;
    - the current running bundle replaces it on every session tool rebind;
    - a digest and dedicated loader identify the trusted payload; and
    - its files are never materialized as the mutable workspace authority.

    Canvas keeps its existing replaceable companion-skill behavior. Capability
    scoping still happens after tools instantiate.
    """

    catalog, menu, files = _normalize_catalog(skills)
    _remove_skill(catalog, APP_GUIDE_SKILL)
    menu = catalog["menu"]
    files = catalog["files"]

    if app_guide_break_glass_disabled():
        logger.warning("Managed app guide disabled by %s", APP_GUIDE_BREAK_GLASS_ENV)
    else:
        try:
            frontmatter, bundled_files = _read_bundled_skill(
                APP_GUIDE_SKILL,
                skills_root=skills_root,
            )
        except (KeyError, OSError, UnicodeDecodeError, ValueError) as exc:
            # Fail closed: never fall back to a same-name user/frozen payload.
            logger.warning("Managed app guide is unavailable: %s", exc)
        else:
            digest = skill_bundle_digest(bundled_files)
            menu.append(
                {
                    "id": APP_GUIDE_SKILL,
                    "name": APP_GUIDE_SKILL,
                    "display_name": frontmatter.get("display_name", "App Guide"),
                    "description": str(
                        frontmatter.get("description", "") or ""
                    ).strip(),
                    "icon": frontmatter.get("icon", "help"),
                    "color": frontmatter.get("color", "#6B7280"),
                    "tags": frontmatter.get("tags", []),
                    "system_managed": True,
                    "loader_tool": APP_GUIDE_LOADER_TOOL,
                    "bundle_digest": digest,
                }
            )
            files[APP_GUIDE_SKILL] = bundled_files
            menu.sort(
                key=lambda item: (
                    str(item.get("name", "")) if isinstance(item, dict) else ""
                )
            )

    return add_default_canvas_skill(catalog, skills_root=skills_root)


def filter_bound_skills(blob: dict[str, Any]) -> dict[str, Any]:
    """Remove skills delivered via deterministic bindings (instruction_files
    ``skill:`` entries) from the model-invoked catalog (menu + files).

    Bound skills are materialized through the flag-independent instructions
    channel (serialize / _deploy_instruction_files), so they must not also be
    offered as optional ``use_skill`` entries. Mutates ``blob`` in place and
    returns it; no-op when there is no skills payload or no bound skills.
    Slice 3.
    """
    skills = blob.get("skills")
    if not skills:
        return blob
    bound = {
        e.get("skill")
        for e in (blob.get("agent", {}).get("instruction_files") or [])
        if e.get("skill")
    }
    if not bound:
        return blob
    if skills.get("menu"):
        skills["menu"] = [m for m in skills["menu"] if m.get("name") not in bound]
    if skills.get("files"):
        skills["files"] = {n: f for n, f in skills["files"].items() if n not in bound}
    return blob


def scope_skills_for_tools(
    skills: dict[str, Any], tool_names: list[str] | set[str]
) -> dict[str, Any]:
    """Return the resolved skill payload allowed by final tool capabilities.

    Most skills are ordinary catalog entries. Managed skills are different:

    - ``app-guide`` requires its immutable, workspace-independent reader; and
    - ``present-with-canvas`` requires both the ordinary skill reader and a
      working file Canvas.

    Keeping this pure lets runtimes apply the rule after their backend gate.
    """
    scoped = copy.deepcopy(skills or {})
    available = set(tool_names)

    if APP_GUIDE_LOADER_TOOL not in available:
        _remove_skill(scoped, APP_GUIDE_SKILL)
    if not {"use_skill", "set_canvas"}.issubset(available):
        _remove_skill(scoped, PRESENT_WITH_CANVAS_SKILL)
    return scoped
