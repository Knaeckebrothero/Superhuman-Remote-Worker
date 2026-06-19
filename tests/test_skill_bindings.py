"""Agent Skills — Slice 3 (expert bindings + migration).

Covers the skill->binding mechanism: InstructionFileEntry's skill: field + path
resolver, the before_tool/phase consumers resolving via entry.path, the
flag-independent serialize channel for bound skills, and the migration of the
two bundled guides (research-guide, todo-guide).

Design: docs/features/agent_skills.md (Slice 3).
Plan:   docs/superpowers/plans/2026-06-19-skills-slice-3.md
"""

import pytest

from src.core.loader import InstructionFileEntry, load_agent_config_from_dict


# ---------------------------------------------------------------------------
# Task 1: InstructionFileEntry skill: field + path resolver + XOR validation
# ---------------------------------------------------------------------------


def test_file_entry_path_is_the_file():
    e = InstructionFileEntry(trigger="before_tool:next_phase_todos", file="todo_guide.md")
    assert e.path == "todo_guide.md"
    assert e.trigger_type == "before_tool"
    assert e.trigger_target == "next_phase_todos"


def test_skill_entry_path_is_skill_md():
    e = InstructionFileEntry(trigger="phase:tactical", skill="research-guide", enforce=False)
    assert e.path == "skills/research-guide/SKILL.md"
    assert e.trigger_type == "phase"
    assert e.trigger_target == "tactical"


def test_entry_requires_exactly_one_of_file_or_skill():
    with pytest.raises(ValueError):
        InstructionFileEntry(trigger="phase:tactical")  # neither
    with pytest.raises(ValueError):
        InstructionFileEntry(trigger="phase:tactical", file="x.md", skill="x")  # both


def test_parse_skill_binding_from_config():
    cfg = load_agent_config_from_dict(
        {
            "agent_id": "t",
            "display_name": "T",
            "instruction_files": [
                {"skill": "research-guide", "trigger": "phase:tactical", "enforce": False},
                {"file": "todo_guide.md", "trigger": "before_tool:next_phase_todos"},
            ],
        }
    )
    by_path = {e.path: e for e in cfg.instruction_files}
    assert "skills/research-guide/SKILL.md" in by_path
    assert by_path["skills/research-guide/SKILL.md"].skill == "research-guide"
    assert by_path["skills/research-guide/SKILL.md"].enforce is False
    assert by_path["todo_guide.md"].file == "todo_guide.md"
    assert by_path["todo_guide.md"].enforce is True  # default
