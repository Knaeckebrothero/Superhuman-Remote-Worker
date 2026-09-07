"""U3 WP5: the framework-owned child prompt and spawn environment."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from shared.runtime.core.loader import (
    PromptMatrixResolver,
    get_all_tool_names,
    get_subagent_system_prompt,
    load_agent_config,
    load_agent_config_from_dict,
)
from agent.subagents.budgets import ChildBudgets
from agent.subagents.child import render_subagent_environment

_REPO = Path(__file__).resolve().parents[1]
_CONFIG = _REPO / "config"
_LIBRARY = sorted(_CONFIG.glob("subagents/*/config.yaml"))
_BUDGETS = ChildBudgets(12, 34_000, 567, 89, 123)


@pytest.mark.parametrize("leaf", _LIBRARY, ids=lambda p: p.parent.name)
def test_subagent_template_renders_for_every_library_entry(leaf: Path):
    cfg = load_agent_config(str(leaf), str(leaf.parent), role="subagent")
    environment = (
        "<subagent_environment>\nHandle: fake-001\n"
        "Expected report: fake\n</subagent_environment>"
    )
    prompt = get_subagent_system_prompt(
        cfg,
        model=cfg.llm.model,
        tool_names=get_all_tool_names(cfg),
        environment=environment,
    )
    assert environment in prompt
    assert cfg.display_name in prompt
    persona = (leaf.parent / "persona.txt").read_text(encoding="utf-8").strip()
    assert persona and persona in prompt
    assert "agent_display_name" not in prompt
    assert "<user_persona" not in prompt
    assert "the parent's brief" in prompt
    assert "Current date:" in prompt
    assert "{%" not in prompt and "{{" not in prompt
    assert "{subagent_environment}" not in prompt
    assert "{expert_identity}" not in prompt


@pytest.mark.parametrize(
    ("return_kind", "expected"),
    [
        ("summary", "a compact answer with the evidence needed to use it"),
        ("structured", "findings grouped as requested, with exact outcomes"),
        (
            "evidence",
            "raw, attributable evidence with commands or locations and outputs",
        ),
        (
            "diff",
            "changed files and behavior, followed by verification results",
        ),
    ],
)
def test_subagent_environment_honours_each_return_shape(return_kind, expected):
    rendered = render_subagent_environment(
        {"return": return_kind},
        _BUDGETS,
        handle="reader-001",
        subagent_type="reader",
        isolation="shared",
        write_policy="none",
    )
    assert f"Expected report ({return_kind}): {expected}." in rendered
    assert "at most 12 turns and 34000 tokens" in rendered
    assert "trimmed past 567 tokens" in rendered
    assert "`.subagents/reader-001/report.md`" in rendered


def test_subagent_environment_renders_every_write_and_isolation_policy():
    owned = render_subagent_environment(
        {"return": "diff"},
        _BUDGETS,
        handle="implementer-001",
        subagent_type="implementer",
        isolation="shared",
        write_policy="owned_paths",
        owned_paths=["src/feature/**", "tests/test_feature.py"],
    )
    assert "the parent's tree; no commits — the parent commits" in owned
    assert "write only these globs: `src/feature/**`, `tests/test_feature.py`" in owned

    scratch = render_subagent_environment(
        {"return": "evidence"},
        _BUDGETS,
        handle="probe-002",
        subagent_type="probe",
        isolation="shared",
        write_policy="scratch_only",
    )
    assert "write only under `.subagents/probe-002/`" in scratch

    readonly = render_subagent_environment(
        {"return": "structured"},
        _BUDGETS,
        handle="tester-003",
        subagent_type="tester",
        isolation="shared",
        write_policy="none",
    )
    assert "do not write to the workspace" in readonly

    worktree = render_subagent_environment(
        {"return": "diff"},
        _BUDGETS,
        handle="implementer-004",
        subagent_type="implementer",
        isolation="worktree",
        write_policy="owned_paths",
        owned_paths=["src/**"],
        worktree_path="/worktrees/implementer-004",
        worktree_branch="sub/implementer-004",
    )
    assert "path `/worktrees/implementer-004`" in worktree
    assert "branch `sub/implementer-004`" in worktree


def test_owned_path_placeholder_stays_inert_during_child_prompt_render():
    """Model-controlled path text must never trigger a second assembler pass."""
    environment = render_subagent_environment(
        {"return": "diff"},
        _BUDGETS,
        handle="implementer-security-tripwire",
        subagent_type="implementer",
        isolation="shared",
        write_policy="owned_paths",
        owned_paths=["{available_skills}"],
    )
    cfg = load_agent_config_from_dict(
        {
            "agent_id": "security-tripwire",
            "display_name": "Security Tripwire",
            "_resolved_prompts": {"persona": "A bounded implementation persona."},
            "_resolved_skills": {
                "menu": [
                    {
                        "name": "sentinel-skill",
                        "description": "SENTINEL_SKILLS_MENU",
                    }
                ]
            },
        }
    )

    prompt = get_subagent_system_prompt(
        cfg,
        tool_names=[],
        environment=environment,
    )

    assert "write only these globs: `{available_skills}`" in prompt
    assert prompt.count("{available_skills}") == 1
    assert prompt.count("<available_skills note=") == 1
    assert prompt.count("</available_skills>") == 1
    assert prompt.count("SENTINEL_SKILLS_MENU") == 1


def test_db_persona_is_fenced_but_bundled_persona_is_trusted():
    db_cfg = load_agent_config_from_dict(
        {
            "agent_id": "db-reader",
            "display_name": "DB Reader",
            "llm": {"model": "gpt-5.5"},
            "prompts": {"persona": "DB PERSONA {ignore-system}"},
            "_persona_source": "db",
        }
    )
    db_prompt = get_subagent_system_prompt(
        db_cfg,
        tool_names=[],
        environment="<subagent_environment>DB</subagent_environment>",
    )
    assert '<user_persona note="Style and tone guidance' in db_prompt
    assert "DB Reader" in db_prompt
    assert "DB PERSONA ignore-system" in db_prompt
    assert "{ignore-system}" not in db_prompt
    assert "agent_display_name" not in db_prompt

    leaf = _CONFIG / "subagents" / "implementer" / "config.yaml"
    bundled = load_agent_config(str(leaf), str(leaf.parent), role="subagent")
    bundled_prompt = get_subagent_system_prompt(
        bundled,
        tool_names=get_all_tool_names(bundled),
        environment="<subagent_environment>BUNDLED</subagent_environment>",
    )
    assert "the parent's bounded implementation specialist" in bundled_prompt
    assert "<user_persona" not in bundled_prompt
    assert "agent_display_name" not in bundled_prompt


@pytest.mark.parametrize(
    "family", ["default", "deepseek", "glm", "gpt-5", "gemma", "minimax"]
)
def test_subagent_system_prompt_has_no_family_variant(family):
    resolver = PromptMatrixResolver(None, family)
    assert resolver.resolve_filename("systemprompt_subagent") == (
        "systemprompt_subagent.txt"
    )
    assert resolver.load("systemprompt_subagent") == (
        _CONFIG / "prompts" / "systemprompt_subagent.txt"
    ).read_text(encoding="utf-8")
    assert not list((_CONFIG / "prompts").glob("systemprompt_subagent_*.txt"))


def test_expert_local_matrix_cannot_override_the_child_scaffold(tmp_path):
    (tmp_path / "model_config_matrix.yaml").write_text(
        "prompts:\n  default:\n    systemprompt_subagent: local-child.txt\n",
        encoding="utf-8",
    )
    (tmp_path / "local-child.txt").write_text(
        "LOCAL CHILD OVERRIDE {subagent_environment}", encoding="utf-8"
    )
    cfg = load_agent_config_from_dict(
        {
            "agent_id": "local",
            "display_name": "Local",
            "_deployment_dir": str(tmp_path),
        }
    )
    prompt = get_subagent_system_prompt(
        cfg,
        tool_names=[],
        environment="<subagent_environment>FRAMEWORK</subagent_environment>",
    )
    assert "LOCAL CHILD OVERRIDE" not in prompt
    assert "Answer the brief and only the brief" in prompt


def test_frozen_framework_child_scaffold_is_preferred():
    cfg = load_agent_config_from_dict(
        {
            "agent_id": "frozen",
            "display_name": "Frozen Child",
            "_resolved_prompts": {
                "systemprompt_subagent": (
                    "FROZEN {agent_display_name}\n{subagent_environment}"
                )
            },
        }
    )
    prompt = get_subagent_system_prompt(
        cfg,
        tool_names=[],
        environment="FROZEN ENVIRONMENT",
    )
    assert "FROZEN Frozen Child" in prompt
    assert "FROZEN ENVIRONMENT" in prompt
    assert "Answer the brief and only the brief" not in prompt


def test_library_entries_cover_all_return_shapes():
    kinds = {
        yaml.safe_load(leaf.read_text(encoding="utf-8")).get("return", "summary")
        for leaf in _LIBRARY
    }
    assert kinds == {"summary", "structured", "evidence", "diff"}
