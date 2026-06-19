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


# ---------------------------------------------------------------------------
# Task 2: before_tool + phase consumers resolve via entry.path
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from tests._fs_backend import FilesystemTestBackend  # noqa: E402
from src.core.workspace import WorkspaceManager  # noqa: E402
from src.tools.context import ToolContext  # noqa: E402
from src.tools.registry import apply_instruction_enforcement  # noqa: E402


def _ctx(tmp_path, entries):
    ws = WorkspaceManager(job_id="t", backend=FilesystemTestBackend(tmp_path))
    ctx = ToolContext(workspace_manager=ws)
    ctx._instruction_files = entries
    ctx._llm_config = None
    return ctx


def test_before_tool_gate_targets_skill_path(tmp_path):
    ctx = _ctx(
        tmp_path,
        [InstructionFileEntry(trigger="before_tool:next_phase_todos", skill="todo-guide")],
    )
    assert ctx.get_enforcement_files("next_phase_todos") == ["skills/todo-guide/SKILL.md"]
    # gate closed until the skill path is read
    assert ctx.check_tool_enforcement("next_phase_todos") is not None
    ctx.record_file_read("skills/todo-guide/SKILL.md")  # what use_skill / read_file record
    assert ctx.check_tool_enforcement("next_phase_todos") is None


def test_phase_binding_targets_skill_path(tmp_path):
    ctx = _ctx(
        tmp_path,
        [InstructionFileEntry(trigger="phase:tactical", skill="research-guide", enforce=False)],
    )
    entries = ctx.get_phase_instruction_files("tactical")
    assert len(entries) == 1 and entries[0].path == "skills/research-guide/SKILL.md"
    assert ctx.get_phase_instruction_files("strategic") == []


def test_apply_enforcement_wrapper_uses_skill_path(tmp_path):
    ctx = _ctx(
        tmp_path,
        [InstructionFileEntry(trigger="before_tool:next_phase_todos", skill="todo-guide")],
    )
    calls = []
    tool = SimpleNamespace(
        name="next_phase_todos", func=lambda *a, **k: (calls.append(1), "OK")[1]
    )
    apply_instruction_enforcement([tool], ctx)
    blocked = tool.func()
    assert "skills/todo-guide/SKILL.md" in blocked and calls == []  # nudged, not run
    ctx.record_file_read("skills/todo-guide/SKILL.md")
    assert tool.func() == "OK" and calls == [1]  # gate opened


# ---------------------------------------------------------------------------
# Task 3: bound-skill content rides the flag-independent instructions channel
# ---------------------------------------------------------------------------

from src.core.loader import serialize_resolved_config  # noqa: E402


def test_serialize_freezes_bound_skill_md(tmp_path):
    cfg = load_agent_config_from_dict(
        {
            "agent_id": "t",
            "display_name": "T",
            "instruction_files": [
                {"skill": "hello-skill", "trigger": "phase:tactical", "enforce": False}
            ],
        }
    )
    cfg._deployment_dir = str(tmp_path)  # matrix/prompt resolvers need a dir; files absent → None
    blob = serialize_resolved_config(cfg)
    # Frozen under the skill name (not "SKILL"), independent of the catalog flag.
    assert "hello-skill" in blob["instructions"]
    assert "Hello Skill" in blob["instructions"]["hello-skill"]
    assert "SKILL" not in blob["instructions"]  # no stem collision
