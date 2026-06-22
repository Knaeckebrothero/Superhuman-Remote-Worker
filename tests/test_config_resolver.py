"""Tests for the orchestrator-side shared config resolver.

``resolve_config`` composes ``src/core/loader`` steps to produce the same
``serialize_resolved_config``-shaped blob the agent hydrates — used by BOTH job
dispatch and session attach. The keystone guarantee is fidelity: the base-only
resolve must be byte-identical to today's ``from_config`` fallback
(``load_agent_config`` → ``serialize_resolved_config``), so turning the flag on
without an expert changes nothing.
"""

import asyncio

from orchestrator.security.access import redact_config_override
from orchestrator.services.config_resolver import (
    inject_blob_credentials,
    resolve_config,
)
from src.core.loader import (
    load_agent_config,
    resolve_config_path,
    serialize_resolved_config,
)


def test_base_only_resolves_to_blob():
    # No expert, no overrides → just the bundled base, serialized.
    blob = resolve_config(base_config_name="persistent_defaults")
    assert "agent" in blob and "prompts" in blob and "instructions" in blob
    assert blob["agent"]["agent_id"]  # base loaded + parsed
    assert "api_key" not in blob["agent"].get("llm", {})  # serialize strips it


def test_base_only_matches_load_agent_config():
    """Fidelity guard: base-only resolve == load_agent_config → serialize.

    Catches any composition drift (especially the settings-matrix
    explicit-llm-keys handling) between the orchestrator resolver and the
    agent's from_config path. The 'agent' sub-dict is deterministic (the
    timestamp lives at the blob top level, not under 'agent').
    """
    path, dep = resolve_config_path("persistent_defaults")
    cfg = load_agent_config(path, dep)
    expected = serialize_resolved_config(cfg, model=cfg.llm.model)

    blob = resolve_config(base_config_name="persistent_defaults")

    assert blob["agent"] == expected["agent"]
    assert blob["prompts"] == expected["prompts"]
    assert blob["instructions"] == expected["instructions"]


def _expert_row(model="gemma-4-moe", persona="SENTINEL-PERSONA", instructions="do X"):
    return {
        "expert_type": "session",
        "name": "sess-helper",
        "config": {"llm": {"model": model}},
        "prompts": {"persona": persona, "instructions": instructions},
    }


def test_expert_fragment_is_base_layer_and_fenced():
    """Expert is the BASE layer; a request override wins on top; the DB persona
    is delivered and the fence marker is set (decision 7)."""
    blob = resolve_config(
        base_config_name="persistent_defaults",
        expert_row=_expert_row(),
        request_override={"llm": {"model": "user-pick"}},  # user wins over expert
        expert_type="session",
    )
    assert blob["agent"]["llm"]["model"] == "user-pick"  # request > expert
    assert blob["agent"]["_persona_source"] == "db"  # fenced (flattened to top level)
    assert blob["prompts"]["persona"] == "SENTINEL-PERSONA"  # persona delivered
    assert blob["prompts"]["instructions"] == "do X"  # instructions delivered


def test_base_defaults_replace_placeholder_below_expert():
    """base_defaults (the system/user default-model floor) replaces the bundled
    placeholder, but an expert still overrides the chat model."""
    blob = resolve_config(
        base_config_name="persistent_defaults",
        base_defaults={
            "llm": {"model": "sys-default"},
            "auxiliary": {"model": "aux-d"},
        },
    )
    assert blob["agent"]["llm"]["model"] == "sys-default"  # floor > base placeholder
    assert blob["agent"]["auxiliary"]["model"] == "aux-d"

    blob2 = resolve_config(
        base_config_name="persistent_defaults",
        base_defaults={"llm": {"model": "sys-default"}},
        expert_row=_expert_row(model="expert-model"),
    )
    assert blob2["agent"]["llm"]["model"] == "expert-model"  # expert > floor


def test_expert_model_applies_when_no_request_override():
    blob = resolve_config(
        base_config_name="persistent_defaults",
        expert_row=_expert_row(model="gemma-4-moe"),
        expert_type="session",
    )
    assert blob["agent"]["llm"]["model"] == "gemma-4-moe"  # expert > base


def test_bundled_base_has_no_persona_source_marker():
    """Only DB experts are fenced — a plain base resolve carries no marker."""
    blob = resolve_config(base_config_name="persistent_defaults")
    assert "_persona_source" not in blob["agent"]


# --- credential delivery / strip-for-persist contract -----------------------


def test_credentials_injected_into_delivery_copy_only():
    """resolve_config returns a secret-free blob (serialize strips llm.api_key);
    delivery injects creds into a COPY — the original (persistable) blob is
    never mutated."""
    blob = resolve_config(base_config_name="persistent_defaults")

    async def fake_injector(co):  # mirrors _inject_dispatch_credentials
        co.setdefault("llm", {})["api_key"] = "sk-secret"
        co["llm"]["base_url"] = "https://router.example"
        return co

    delivered = asyncio.run(inject_blob_credentials(blob, fake_injector))

    assert delivered["agent"]["llm"]["api_key"] == "sk-secret"
    assert delivered["agent"]["llm"]["base_url"] == "https://router.example"
    # The persistable blob stays clean.
    assert "api_key" not in blob["agent"].get("llm", {})


def test_redact_strips_secrets_from_blob_for_persist():
    """The persisted copy goes through redact_config_override (the canonical
    strip), which removes api_key / *_API_KEY anywhere while keeping non-secrets."""
    blob = {
        "agent": {
            "llm": {"model": "m", "api_key": "sk-x"},
            "env_keys": {"FOO_API_KEY": "s", "FOO_MODEL": "m"},
        }
    }
    persisted = redact_config_override(blob)
    assert "api_key" not in persisted["agent"]["llm"]
    assert "FOO_API_KEY" not in persisted["agent"]["env_keys"]
    assert persisted["agent"]["env_keys"]["FOO_MODEL"] == "m"  # non-secret preserved


# --- fail-fast: transport-less pinned models (Option D) ---------------------


def test_unrouted_model_slots_flags_transportless_phase_pin():
    """A pinned model with NO base_url / api_key / provider after injection would
    silently fall back to api.openai.com and 401/404 (eec20eeb). Flag it so
    dispatch can fail fast with an actionable error instead."""
    from orchestrator.services.config_resolver import unrouted_model_slots

    blob = {
        "agent": {
            "llm": {
                "model": "gemma",
                "base_url": "http://router/v1",  # base: routed
                "strategic": {"model": "gpt-5.5"},  # UNROUTED — no transport
                "tactical": {  # routed via provider + key
                    "model": "gpt-5.4-mini",
                    "provider": "openai",
                    "api_key": "sk-x",
                },
            },
            "auxiliary": {"model": "aux", "base_url": "http://aux/v1"},  # routed
        }
    }
    problems = unrouted_model_slots(blob)
    assert any("strategic" in p and "gpt-5.5" in p for p in problems)
    assert not any("tactical" in p for p in problems)
    assert not any(p.startswith("llm model") for p in problems)
    assert not any("auxiliary" in p for p in problems)


def test_unrouted_model_slots_empty_when_all_routed():
    from orchestrator.services.config_resolver import unrouted_model_slots

    blob = {"agent": {"llm": {"model": "m", "base_url": "u"}}}
    assert unrouted_model_slots(blob) == []


def test_unrouted_model_slots_ignores_slots_without_a_model():
    """Empty/absent model sections are not failures (the base may legitimately
    omit a phase pin)."""
    from orchestrator.services.config_resolver import unrouted_model_slots

    blob = {"agent": {"llm": {"model": "m", "base_url": "u", "strategic": {}}}}
    assert unrouted_model_slots(blob) == []
