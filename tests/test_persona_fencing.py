"""Decision-7 render-side persona fencing.

A DB-authored persona is untrusted user content. When ``_persona_source == "db"``
the render path must wrap it via ``fence_persona`` (``<user_persona>`` frame,
subordinated below operator policy) before injecting it as ``expert_identity`` —
never at system altitude. This was removed by ``6f8c635e`` and is restored here
in the orchestrator-resolved shape (the marker rides in the resolved blob).
"""

from src.core.loader import (
    get_phase_system_prompt,
    load_agent_config_from_dict,
)


def _config(_persona_source=None, **resolved_prompts):
    data = {
        "agent_id": "t",
        "display_name": "T",
        "_resolved_prompts": resolved_prompts,
    }
    if _persona_source is not None:
        data["_persona_source"] = _persona_source
    return load_agent_config_from_dict(data)


def test_db_persona_is_fenced_in_interactive_prompt():
    config = _config(
        _persona_source="db",
        systemprompt_interactive="SYS {agent_display_name} :: {expert_identity}",
        persona="Talk like a pirate.",
    )
    out = get_phase_system_prompt(config, is_strategic=False, prompt_type="interactive")
    assert "<user_persona" in out
    assert "Talk like a pirate." in out


def test_db_persona_is_fenced_in_worker_prompt():
    config = _config(
        _persona_source="db",
        systemprompt="BASE {agent_display_name} ID:{expert_identity} C:{prompt_content}",
        persona="Talk like a pirate.",
        tactical="TAC{phase_number}",
    )
    out = get_phase_system_prompt(config, is_strategic=False)
    assert "<user_persona" in out
    assert "Talk like a pirate." in out


def test_file_persona_is_not_fenced():
    """A bundled (file/config) persona carries no marker → injected verbatim."""
    config = _config(
        systemprompt_interactive="SYS {agent_display_name} :: {expert_identity}",
        persona="Talk like a pirate.",
    )
    out = get_phase_system_prompt(config, is_strategic=False, prompt_type="interactive")
    assert "<user_persona" not in out
    assert "Talk like a pirate." in out
