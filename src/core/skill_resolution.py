"""Pure resolution helpers for the Agent Skills runtime (Slice 2).

Kept separate from main.py / loader.py so the menu-precedence and workspace-
mapping logic is small and unit-testable in isolation (mirrors
``expert_resolution.py``). No DB or framework imports here.

Design: docs/features/agent_skills.md (Slice 2).
"""

from __future__ import annotations

from typing import Any

from src.core.expert_resolution import expert_precedence_key


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
