"""Resolution helpers for the Agent Skills runtime (Slice 2).

Kept separate from main.py / loader.py so the menu-precedence and workspace-
mapping logic is small and unit-testable in isolation (mirrors
``expert_resolution.py``). No DB or framework imports live here; the one disk
read is the explicit bundled Canvas-skill floor.

Design: docs/features/agent_skills.md (Slice 2).
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any

from src.core.expert_resolution import expert_precedence_key

logger = logging.getLogger(__name__)

PRESENT_WITH_CANVAS_SKILL = "present-with-canvas"


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

    catalog = copy.deepcopy(skills or {})
    menu = catalog.get("menu")
    files = catalog.get("files")
    if not isinstance(menu, list):
        menu = []
        catalog["menu"] = menu
    if not isinstance(files, dict):
        files = {}
        catalog["files"] = files

    if (
        any(
            isinstance(item, dict) and item.get("name") == PRESENT_WITH_CANVAS_SKILL
            for item in menu
        )
        or PRESENT_WITH_CANVAS_SKILL in files
    ):
        return catalog

    root = skills_root or Path(__file__).resolve().parents[2] / "config" / "skills"
    skill_dir = root / PRESENT_WITH_CANVAS_SKILL
    try:
        from src.core.skill_format import (
            parse_skill_md,
            skill_identity,
            validate_skill_path,
        )

        bundled_files: dict[str, str] = {}
        for path in sorted(skill_dir.rglob("*")):
            if not path.is_file():
                continue
            rel_path = path.relative_to(skill_dir).as_posix()
            validate_skill_path(rel_path)
            bundled_files[rel_path] = path.read_text(encoding="utf-8")
        frontmatter, _ = parse_skill_md(bundled_files["SKILL.md"])
        name, description = skill_identity(frontmatter)
        if name != PRESENT_WITH_CANVAS_SKILL:
            raise ValueError(
                f"Bundled Canvas skill name must be {PRESENT_WITH_CANVAS_SKILL!r}"
            )
    except (KeyError, OSError, UnicodeDecodeError, ValueError) as exc:
        logger.warning("Bundled Canvas companion skill is unavailable: %s", exc)
        return catalog

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

    Most skills are ordinary catalog entries. ``present-with-canvas`` is
    different: it must not appear in the menu or workspace unless the model can
    both load skills and actually publish a file Canvas. Keeping this pure lets
    worker and persistent runtimes apply the rule after their backend gate.
    """
    scoped = copy.deepcopy(skills or {})
    available = set(tool_names)
    if {"use_skill", "set_canvas"}.issubset(available):
        return scoped

    name = PRESENT_WITH_CANVAS_SKILL
    if isinstance(scoped.get("menu"), list):
        scoped["menu"] = [
            item
            for item in scoped["menu"]
            if not isinstance(item, dict) or item.get("name") != name
        ]
    if isinstance(scoped.get("files"), dict):
        scoped["files"].pop(name, None)
    return scoped
