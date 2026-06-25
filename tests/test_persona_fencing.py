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


def _config(_persona_source=None, _db_prompt_keys=None, **resolved_prompts):
    data = {
        "agent_id": "t",
        "display_name": "T",
        "_resolved_prompts": resolved_prompts,
    }
    if _persona_source is not None:
        data["_persona_source"] = _persona_source
    if _db_prompt_keys is not None:
        data["_db_prompt_keys"] = _db_prompt_keys
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


# ── Part 2: phase-directive fencing + brace-safety (render side) ──────────

_BASE = "BASE {agent_display_name} ID:{expert_identity} C:{prompt_content}"


def test_db_phase_directive_is_fenced_in_worker_prompt():
    config = _config(
        _db_prompt_keys=["tactical"],
        systemprompt=_BASE,
        tactical="Run the tests, then ship.",
    )
    out = get_phase_system_prompt(config, is_strategic=False)
    assert "<expert_workflow" in out
    assert "Run the tests, then ship." in out


def test_db_phase_directive_with_literal_braces_does_not_crash():
    """A user strategic/tactical containing literal { } (e.g. a JSON example) must
    not break the str.format() in the prompt assembler (the brace-crash guard)."""
    config = _config(
        _db_prompt_keys=["strategic"],
        systemprompt=_BASE,
        strategic='Emit {"status": "ok", "n": 1} and stop.',
    )
    out = get_phase_system_prompt(config, is_strategic=True)  # must not raise
    assert "<expert_workflow" in out
    assert '"status": "ok"' in out  # content survives (only braces stripped)


def test_inherited_phase_directive_is_not_fenced():
    """No _db_prompt_keys entry → trusted disk phase prompt: verbatim, with
    {phase_number} still substituted."""
    config = _config(systemprompt=_BASE, tactical="TAC{phase_number}")
    out = get_phase_system_prompt(config, is_strategic=False, phase_number=2)
    assert "<expert_workflow" not in out
    assert "TAC2" in out


def test_db_summarization_braces_survive_format_map():
    """A DB summarization prompt with literal braces must pass the summarizer's
    format_map(defaultdict(str)) without raising (auxiliary.py contract)."""
    from collections import defaultdict

    from src.core.loader import load_summarization_prompt

    config = _config(
        _db_prompt_keys=["summarization"],
        summarization='Summarize. Keep JSON like {"k": 1} intact.',
    )
    template = load_summarization_prompt(config)
    rendered = template.format_map(  # must not raise
        defaultdict(str, conversation="", max_summary_length="10000")
    )
    assert '{"k": 1}' in rendered  # literal braces preserved after un-escaping
