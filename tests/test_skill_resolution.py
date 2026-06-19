"""Pure skill-menu resolution + fencing (Agent Skills, Slice 2).

Mirrors the experts precedence model (src/core/expert_resolution.py) but the
menu keeps ALL names (bundled is the floor) instead of picking one winner.
"""

import pytest

from src.core.skill_resolution import resolve_skill_menu, skill_files_to_workspace
from src.core.expert_resolution import fence_skills_menu

U = "11111111-1111-1111-1111-111111111111"


def _row(name, *, owner_id=None, is_global=False, created_at="2026-01-01", **extra):
    return {
        "name": name,
        "description": f"desc-{name}",
        "owner_id": owner_id,
        "is_global": is_global,
        "created_at": created_at,
        **extra,
    }


def test_menu_keeps_bundled_floor():
    rows = [_row("a", _source="bundled")]
    menu = resolve_skill_menu(rows, user_id=U, project_ids=set())
    assert [m["name"] for m in menu] == ["a"]
    assert menu[0]["_source"] == "bundled"


def test_owner_shadows_bundled_same_name():
    rows = [
        _row("a", _source="bundled"),
        _row("a", owner_id=U, _source="user"),
    ]
    menu = resolve_skill_menu(rows, user_id=U, project_ids=set())
    assert len(menu) == 1
    assert menu[0]["_source"] == "user"  # owner (tier 3) wins


def test_global_shadows_bundled_but_not_owner():
    rows = [
        _row("a", _source="bundled"),
        _row("a", is_global=True, _source="global"),
        _row("a", owner_id=U, _source="user"),
    ]
    menu = resolve_skill_menu(rows, user_id=U, project_ids=set())
    assert len(menu) == 1 and menu[0]["_source"] == "user"


def test_menu_is_sorted_by_name_deterministic():
    rows = [_row("zeta"), _row("alpha"), _row("mid", owner_id=U)]
    names = [m["name"] for m in resolve_skill_menu(rows, user_id=U, project_ids=set())]
    assert names == ["alpha", "mid", "zeta"]


def test_files_to_workspace_prefixes_skill_dir():
    out = skill_files_to_workspace(
        {"hello": {"SKILL.md": "x", "references/g.md": "y"}}
    )
    assert out == {
        "skills/hello/SKILL.md": "x",
        "skills/hello/references/g.md": "y",
    }


def test_fence_skills_menu_empty_is_blank():
    assert fence_skills_menu([]) == ""


def test_fence_skills_menu_wraps_and_strips_braces():
    out = fence_skills_menu([{"name": "a", "description": "use {when} ok"}])
    assert "<available_skills" in out and "</available_skills>" in out
    assert "- a: use when ok" in out  # braces stripped
    assert "{" not in out and "}" not in out


# --- Slice 3: bound skills are removed from the model-invoked catalog ---

from src.core.skill_resolution import filter_bound_skills  # noqa: E402


def test_filter_removes_bound_skill_from_menu_and_files():
    blob = {
        "agent": {
            "instruction_files": [
                {"skill": "todo-guide", "trigger": "before_tool:next_phase_todos"},
                {"file": "x.md", "trigger": "phase:tactical"},
            ]
        },
        "skills": {
            "menu": [{"name": "todo-guide"}, {"name": "free-skill"}],
            "files": {"todo-guide": {"SKILL.md": "x"}, "free-skill": {"SKILL.md": "y"}},
        },
    }
    filter_bound_skills(blob)
    assert [m["name"] for m in blob["skills"]["menu"]] == ["free-skill"]
    assert set(blob["skills"]["files"]) == {"free-skill"}


def test_filter_noop_without_skills_or_bindings():
    assert filter_bound_skills({"agent": {}}) == {"agent": {}}
    blob = {"agent": {"instruction_files": []}, "skills": {"menu": [{"name": "a"}], "files": {}}}
    filter_bound_skills(blob)
    assert [m["name"] for m in blob["skills"]["menu"]] == ["a"]  # nothing bound → unchanged
