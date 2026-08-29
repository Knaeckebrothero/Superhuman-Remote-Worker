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


def test_account_fallback_sits_below_bundled_expert_leaf():
    """Bundled experts are overlays too; their explicit values beat account defaults."""
    cap: dict = {}
    resolve_config(
        base_config_name="developer",
        base_defaults={"llm": {"reasoning_level": "low"}},
        capture=cap,
        expert_type="worker",
    )
    assert cap["merged_fragment"]["llm"]["reasoning_level"] == "high"


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
    assert "_db_prompt_keys" not in blob["agent"]


# --- Part 2: DB-expert phase-prompt parity ----------------------------------


def test_expert_overlays_phase_prompts_and_marks_db_keys():
    """A DB expert carrying strategic/tactical/summarization gets them overlaid
    into the blob, and each overridden segment is recorded in _db_prompt_keys so
    the render path fences the untrusted phase content."""
    row = {
        "expert_type": "session",
        "name": "sess-helper",
        "config": {"llm": {"model": "gemma-4-moe"}},
        "prompts": {
            "persona": "P",
            "instructions": "I",
            "strategic": "STRAT-SENTINEL",
            "tactical": "TAC-SENTINEL",
            "summarization": "SUMM-SENTINEL",
        },
    }
    blob = resolve_config(
        base_config_name="persistent_defaults",
        expert_row=row,
        expert_type="session",
    )
    assert blob["prompts"]["strategic"] == "STRAT-SENTINEL"
    assert blob["prompts"]["tactical"] == "TAC-SENTINEL"
    assert blob["prompts"]["summarization"] == "SUMM-SENTINEL"
    assert set(blob["agent"]["_db_prompt_keys"]) == {
        "persona",
        "instructions",
        "strategic",
        "tactical",
        "summarization",
    }


def test_inherited_phase_prompts_are_not_marked_db():
    """An expert overriding only persona must NOT mark strategic/tactical as
    DB-authored — those inherit the trusted disk default and stay unfenced."""
    row = {
        "expert_type": "session",
        "name": "sess-helper",
        "config": {},
        "prompts": {"persona": "only persona"},
    }
    blob = resolve_config(
        base_config_name="persistent_defaults",
        expert_row=row,
        expert_type="session",
    )
    assert blob["agent"]["_db_prompt_keys"] == ["persona"]
    # The blob still carries a (disk-resolved) strategic — just not DB-marked.
    assert "strategic" not in blob["agent"]["_db_prompt_keys"]


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


def test_unrouted_model_slots_flags_transportless_summarization_pin():
    """A pinned model with NO base_url / api_key / provider after injection would
    silently fall back to api.openai.com and 401/404 (eec20eeb). Flag it so
    dispatch can fail fast with an actionable error instead. Since U1 the
    model-bearing slots are llm, llm.summarization and auxiliary."""
    from orchestrator.services.config_resolver import unrouted_model_slots

    blob = {
        "agent": {
            "llm": {
                "model": "gemma",
                "base_url": "http://router/v1",  # base: routed
                "summarization": {"model": "gpt-5.5"},  # UNROUTED — no transport
            },
            "auxiliary": {"model": "aux", "base_url": "http://aux/v1"},  # routed
        }
    }
    problems = unrouted_model_slots(blob)
    assert any("summarization" in p and "gpt-5.5" in p for p in problems)
    assert not any(p.startswith("llm model") for p in problems)
    assert not any("auxiliary" in p for p in problems)

    routed = {
        "agent": {
            "llm": {
                "model": "gemma",
                "base_url": "http://router/v1",
                "summarization": {  # routed via provider + key
                    "model": "gpt-5.4-mini",
                    "provider": "openai",
                    "api_key": "sk-x",
                },
            }
        }
    }
    assert unrouted_model_slots(routed) == []


def test_unrouted_model_slots_sees_lifted_legacy_pin_at_top_level():
    """A legacy transport-less tactical pin in the request override is lifted
    into llm.model by resolve_config, so the fail-fast names the TOP-LEVEL slot
    (the nested phase check is gone with the tiers)."""
    from orchestrator.services.config_resolver import unrouted_model_slots

    blob = resolve_config(
        base_config_name="defaults",
        request_override={"llm": {"tactical": {"model": "orphan-pin"}}},
        expert_type="worker",
    )
    assert blob["agent"]["llm"]["model"] == "orphan-pin"
    assert "tactical" not in blob["agent"]["llm"]
    problems = unrouted_model_slots(blob)
    assert "llm model 'orphan-pin'" in problems
    assert not any("tactical" in p for p in problems)


def test_unrouted_model_slots_empty_when_all_routed():
    from orchestrator.services.config_resolver import unrouted_model_slots

    blob = {"agent": {"llm": {"model": "m", "base_url": "u"}}}
    assert unrouted_model_slots(blob) == []


def test_unrouted_model_slots_ignores_slots_without_a_model():
    """Empty/absent model sections are not failures (the base may legitimately
    omit a summarization override)."""
    from orchestrator.services.config_resolver import unrouted_model_slots

    blob = {"agent": {"llm": {"model": "m", "base_url": "u", "summarization": {}}}}
    assert unrouted_model_slots(blob) == []


# --- Per-model context-window override flows to derived limits (blob path) ----


def test_request_override_context_window_drives_derived_limits():
    """A per-model context window passed via ``request_override`` becomes the
    limits-derivation base: it sets BOTH ``limits.model_max_context_tokens`` and
    the 0.80x ``context_threshold_tokens``, overriding the family default.

    This is the mechanism the orchestrator's ``_seed_registry_model_overrides``
    relies on so an admin's Admin -> Models ``context_window`` survives the blob
    dispatch path. Regression guard for
    ``knowledge-base/knowledge/issues/per_model_context_window_override_shadowed_in_blob_dispatch.md``
    (job 19707fa1: minimax-m3 baked 1000000/800000 despite a registry cap of
    262144). Matrix-independent: the override value itself drives the result.
    """
    OVERRIDE = 262144  # an admin 256k cap, distinct from the 1M family default
    blob = resolve_config(
        base_config_name="persistent_defaults",
        request_override={
            "llm": {
                "model": "openrouter/minimax/minimax-m3",
                "model_max_context_tokens": OVERRIDE,
            }
        },
        expert_type="worker",
    )
    limits = blob["agent"]["limits"]
    assert limits["model_max_context_tokens"] == OVERRIDE
    assert limits["context_threshold_tokens"] == int(OVERRIDE * 0.80)  # 209715


def test_no_per_model_window_falls_back_to_family_default():
    """Without a per-model override the model resolves to the (larger) family
    default window — proving the override, not a coincidental family match, is
    what changed the derived limits above. The bug was exactly this default
    silently winning on the blob path (minimax-m3 family default = 1M, so
    compaction fired at 800k instead of the admin's ~205k)."""
    OVERRIDE = 262144
    blob = resolve_config(
        base_config_name="persistent_defaults",
        request_override={"llm": {"model": "openrouter/minimax/minimax-m3"}},
        expert_type="worker",
    )
    limits = blob["agent"]["limits"]
    # family default (1M) > the admin cap — the "compaction fires too late" bug
    assert limits["model_max_context_tokens"] > OVERRIDE
    assert limits["context_threshold_tokens"] == int(
        limits["model_max_context_tokens"] * 0.80
    )


# --- U1 WP2: role re-rooting, role-wins on root names, $ignore_keys pruning ---


def test_worker_base_only_matches_load_agent_config():
    """Worker twin of the fidelity guard: the worker root is expert_base +
    the worker overlay, and the resolver's explicit-llm-key handling for that
    pair must equal the agent's from_config path."""
    path, dep = resolve_config_path("worker_base")
    cfg = load_agent_config(path, dep)
    expected = serialize_resolved_config(cfg, model=cfg.llm.model)

    blob = resolve_config(base_config_name="worker_base", expert_type="worker")

    assert blob["agent"] == expected["agent"]
    assert blob["prompts"] == expected["prompts"]
    assert blob["instructions"] == expected["instructions"]


def test_session_expert_as_worker_gets_worker_keys():
    """A session expert (assistant extends session_base) dispatched as a
    worker re-roots onto the worker overlay: it gains the phase loop's keys."""
    cap: dict = {}
    blob = resolve_config(
        base_config_name="assistant", expert_type="worker", capture=cap
    )
    merged = cap["merged_fragment"]
    assert blob["agent"]["agent_id"] == "assistant"
    assert merged["phase_settings"]["min_todos"] == 2
    assert merged["autonomy"] == "review"
    assert "next_phase_todos" in merged["tools"]["core"]
    assert merged["llm"]["max_retries"] == 0


def test_worker_expert_as_session_uses_session_overlay_and_drops_nothing():
    cap: dict = {}
    blob = resolve_config(
        base_config_name="developer", expert_type="session", capture=cap
    )
    merged = cap["merged_fragment"]
    assert blob["agent"]["agent_id"] == "developer"
    assert merged["llm"]["max_retries"] == 3
    assert "get_canvas" in merged["tools"]["canvas"]
    assert merged["memory"]["pipeline"]["writers"][0] == "persistent_interval_extractor"
    # expert wins: the developer's own keys survive, session-relevant or not
    assert merged["tools"]["shell"]
    assert merged["delegation"]["enabled"] is True


def test_role_wins_over_a_root_base_name():
    """The roots are one thing in different roles: a job that names
    ``session_base`` resolves the worker base, and vice versa."""
    worker = resolve_config(base_config_name="session_base", expert_type="worker")
    assert worker["agent"]["agent_id"] == "worker_base"
    session = resolve_config(base_config_name="worker_base", expert_type="session")
    assert session["agent"]["agent_id"] == "session_base"
    # a non-role expert_type keeps the chain's own root (call-site intent only)
    as_is = resolve_config(base_config_name="session_base", expert_type="preview")
    assert as_is["agent"]["agent_id"] == "session_base"


def test_ignored_keys_pruned_after_request_layers():
    """A job override re-adding a key the subagent role ignores is pruned
    again after the request layers (pruning point 2 of 3)."""
    cap: dict = {}
    blob = resolve_config(
        base_config_name="critic",
        expert_type="subagent",
        request_override={
            "workspace": {"backend": "vm", "max_read_words": 123},
            "autonomy": "full",
            "verification": {"enabled": True},
        },
        capture=cap,
    )
    merged = cap["merged_fragment"]
    assert "backend" not in merged["workspace"]
    assert merged["workspace"]["max_read_words"] == 123  # not ignored: kept
    assert "autonomy" not in merged and "verification" not in merged
    assert merged["tools"]["shell"]  # the critic's own tools survive
    assert "$ignore_keys" not in blob["agent"]
    assert "verification" not in blob["agent"]


def test_bundled_expert_base_layer_keeps_the_frameworks_explicit_llm_keys():
    """Pre-split resolver behaviour, pinned: the framework base's own llm keys
    (now authored across expert_base + the overlay) are explicit under a
    bundled expert, so a family default does not clobber the base's
    temperature at dispatch. (This differs from ``load_agent_config(expert)``,
    where only the leaf's keys are explicit — a long-standing resolver
    property the split must not silently change either way.)"""
    blob = resolve_config(
        base_config_name="developer",
        request_override={"llm": {"model": "openai/minimax-m2.7"}},
        expert_type="worker",
    )
    assert blob["agent"]["llm"]["model"] == "openai/minimax-m2.7"
    assert blob["agent"]["llm"]["temperature"] == 0.0  # base-authored, kept
    assert blob["agent"]["llm"]["top_p"] == 0.95  # matrix-owned, applied
