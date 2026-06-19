"""Agent Skills runtime (Slice 2): blob round-trip + Layer-1 menu injection.

The orchestrator attaches the in-scope skills payload to the resolved blob
(``resolve_config``); the agent hydrates it (``load_config_from_resolved``) and
the render path injects a fenced menu into the system prompt
(``get_phase_system_prompt``). Mirrors tests/test_resolved_config_hydrate.py and
tests/test_persona_fencing.py.
"""

from orchestrator.services.config_resolver import resolve_config
from src.core.loader import (
    get_phase_system_prompt,
    load_agent_config_from_dict,
    load_config_from_resolved,
)

PAYLOAD = {
    "menu": [{"name": "hello-skill", "description": "Use when testing."}],
    "files": {"hello-skill": {"SKILL.md": "---\nname: hello-skill\n---\nbody"}},
}


# ── Task 2: blob carries skills (resolver attach + agent hydrate) ──────────────
def test_resolve_config_attaches_skills_and_agent_hydrates():
    blob = resolve_config(base_config_name="persistent_defaults", skills=PAYLOAD)
    assert blob["skills"] == PAYLOAD

    # load_config_from_resolved is exactly what UniversalAgent.from_resolved calls.
    config = load_config_from_resolved(blob)
    assert config.extra["_resolved_skills"] == PAYLOAD


def test_resolve_config_without_skills_has_empty_dict():
    blob = resolve_config(base_config_name="persistent_defaults")
    assert blob.get("skills") in (None, {})
    config = load_config_from_resolved(blob)
    assert config.extra["_resolved_skills"] == {}


# ── Task 3: Layer-1 system-prompt menu injection ──────────────────────────────
def _worker_cfg(template, menu=None):
    data = {
        "agent_id": "t",
        "display_name": "T",
        "_resolved_prompts": {
            "systemprompt": template,
            "tactical": "TAC{phase_number}",
        },
    }
    if menu is not None:
        data["_resolved_skills"] = {"menu": menu}
    return load_agent_config_from_dict(data)


def test_worker_prompt_includes_fenced_skills_menu():
    cfg = _worker_cfg(
        "BASE {agent_display_name} ID:{expert_identity} "
        "SK:{available_skills} C:{prompt_content}",
        menu=[{"name": "hello-skill", "description": "Use when testing."}],
    )
    out = get_phase_system_prompt(cfg, is_strategic=False)
    assert "<available_skills" in out
    assert "- hello-skill: Use when testing." in out


def test_worker_prompt_no_menu_when_no_skills():
    cfg = _worker_cfg(
        "BASE {agent_display_name} ID:{expert_identity} "
        "SK:{available_skills} C:{prompt_content}",
    )
    out = get_phase_system_prompt(cfg, is_strategic=False)
    assert "<available_skills" not in out
    assert "SK: C:" in out  # placeholder resolved to empty string


def test_interactive_prompt_includes_fenced_skills_menu():
    cfg = load_agent_config_from_dict(
        {
            "agent_id": "t",
            "display_name": "T",
            "_resolved_prompts": {
                "systemprompt_interactive": "SYS {agent_display_name} :: "
                "{expert_identity} :: {available_skills}",
            },
            "_resolved_skills": {
                "menu": [{"name": "hello-skill", "description": "Use when testing."}]
            },
        }
    )
    out = get_phase_system_prompt(cfg, is_strategic=False, prompt_type="interactive")
    assert "<available_skills" in out
    assert "- hello-skill: Use when testing." in out
