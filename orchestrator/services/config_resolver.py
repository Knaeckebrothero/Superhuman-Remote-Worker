"""Single source of agent config resolution (supersedes agent-side Decision 6).

Composes ``src/core/loader`` steps so the orchestrator produces the full, frozen
config blob the agent hydrates. Reused by job dispatch AND session attach —
identical resolution; only timing/delivery differ. See
``docs/superpowers/specs/2026-06-17-orchestrator-resolved-config-design.md``.

Pure + synchronous: no DB, no network. The caller fetches the expert row and
passes it in; injects credentials into the *delivery* copy afterwards (the
serialized blob is already free of ``llm.api_key``); and strips any remaining
secrets with ``redact_config_override`` before persisting.
"""

from __future__ import annotations

import copy
from typing import Awaitable, Callable, Optional

import yaml

from src.core.loader import (
    _apply_settings_matrix,
    deep_merge,
    load_agent_config_from_dict,
    load_and_merge_config,
    resolve_config_path,
    serialize_resolved_config,
)


def _raw_leaf_llm_keys(config_path: str) -> set:
    """llm keys explicitly set in the base config's own leaf file.

    Mirrors ``load_agent_config`` (loader.py:1831-1837): the settings matrix
    refines only the keys it owns and must never clobber an explicitly-set llm
    value. Without this the base-only resolve would diverge from the agent's
    ``from_config`` fallback.
    """
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return set((raw.get("llm") or {}).keys())
    except Exception:
        return set()


def resolve_config(
    *,
    base_config_name: str,
    base_defaults: Optional[dict] = None,
    expert_row: Optional[dict] = None,
    project_overrides: Optional[dict] = None,
    db_overrides: Optional[dict] = None,
    user_settings: Optional[dict] = None,
    request_override: Optional[dict] = None,
    expert_type: str = "session",
    capture: Optional[dict] = None,
) -> dict:
    """Resolve the full agent config to a ``serialize_resolved_config``-shaped blob.

    Layer order (global_expert_management.md:246-260, most-specific wins)::

        bundled base -> base_defaults -> expert fragment
        -> project_experts.config_override -> DB config_overrides (0022)
        -> user persistent_agent settings -> request config_override

    ``base_defaults`` sits just above the bundled base and *below* the expert:
    use it for the system/user **default model** selection (model names only), so
    the base config's placeholder model is replaced by the real default while an
    expert or request override still wins. Transport (base_url/api_key) for the
    final model is injected into the delivery copy by ``inject_blob_credentials``.

    The returned blob carries NO credentials (``serialize_resolved_config``
    strips ``llm.api_key``); the caller injects creds into the delivery copy and
    strips the rest before persisting. ``expert_type`` documents the call-site
    intent (the base is actually selected by ``base_config_name``).
    """
    base_path, deployment_dir = resolve_config_path(base_config_name)
    data = load_and_merge_config(base_path)  # bundled base + $extends

    # Protect explicitly-set llm keys from the settings matrix: the base leaf's
    # own keys (mirrors load_agent_config) plus every overlay layer's keys.
    explicit_llm_keys = _raw_leaf_llm_keys(base_path)

    # Default-model floor: replace the base placeholder model before the expert
    # merges (the expert/request still override it).
    if base_defaults:
        if base_defaults.get("llm"):
            explicit_llm_keys |= set(base_defaults["llm"].keys())
        data = deep_merge(data, base_defaults)

    # Expert fragment is the BASE overlay (below project/db/user/request layers,
    # decision 24 replacement-not-merge onto the type base). Mirrors the retired
    # agent-side _apply_db_expert; the caller picks base_config_name by type.
    prompts_override: dict = {}
    if expert_row is not None:
        from src.core.expert_resolution import build_expert_config

        expert_cfg = expert_row.get("config") or {}
        if isinstance(expert_cfg, str):
            import json

            expert_cfg = json.loads(expert_cfg)
        explicit_llm_keys |= set((expert_cfg.get("llm") or {}).keys())
        data, prompts_override = build_expert_config(data, expert_row)
        # decision 7: mark the DB-authored persona so the render path fences it.
        data["_persona_source"] = "db"

    for layer in (project_overrides, db_overrides, user_settings, request_override):
        if layer:
            if layer.get("llm"):
                explicit_llm_keys |= set(layer["llm"].keys())
            data = deep_merge(data, layer)

    _apply_settings_matrix(data, explicit_llm_keys, deployment_dir)

    if capture is not None:
        # Full merged config in fragment shape — the policy view for the dispatch
        # PEP (single PDP). The base's shell/delegation are present here; deny-by-
        # default is reconciled by grandfathering existing users (migration 0030).
        capture["merged_fragment"] = copy.deepcopy(data)

    config = load_agent_config_from_dict(data, deployment_dir=deployment_dir)
    blob = serialize_resolved_config(config, model=config.llm.model)

    # Overlay the DB expert's persona/instructions onto the resolved prompts so
    # the frozen blob matches what the render path emits: serialize_resolved_config
    # only sees disk + config, never the expert row's out-of-band prompts. Fenced
    # at render via the _persona_source marker (decision 7).
    for _k in ("persona", "instructions"):
        if prompts_override.get(_k):
            blob["prompts"][_k] = prompts_override[_k]

    return blob


async def inject_blob_credentials(
    blob: dict,
    injector: Callable[[dict], Awaitable[dict] | Awaitable[None]],
) -> dict:
    """Return a DELIVERY copy of ``blob`` with LLM / auxiliary / env credentials
    injected. The input blob (the persistable copy) is never mutated and stays
    secret-free — credentials live only on the returned copy.

    ``injector`` is an async callable taking a ``config_override``-shaped dict
    (``{"llm": {...}, "auxiliary": {...}, "env_keys": {...}}``) and enriching it
    in place (and/or returning it): i.e. the orchestrator's existing
    ``_inject_dispatch_credentials`` (jobs) or ``_inject_thread_dispatch_credentials``
    (sessions). Seeding the override from the blob's resolved model means the
    injector routes credentials for the *resolved* model, not the YAML default.
    """
    delivered = copy.deepcopy(blob)
    agent = delivered.setdefault("agent", {})

    co: dict = {"llm": dict(agent.get("llm") or {})}
    if agent.get("auxiliary"):
        co["auxiliary"] = dict(agent["auxiliary"])
    if agent.get("env_keys"):
        co["env_keys"] = dict(agent["env_keys"])

    # Drop None-valued model keys so the injector's gap-fill / setdefault logic
    # treats them as absent (a serialized config carries explicit model=None /
    # base_url=None defaults that would otherwise block injection). Mirrors
    # _inject_thread_dispatch_credentials' own None-stripping.
    for _sect in ("llm", "auxiliary"):
        _s = co.get(_sect)
        if isinstance(_s, dict):
            for _k in [_k for _k, _v in _s.items() if _v is None]:
                del _s[_k]

    result = await injector(co)
    if isinstance(result, dict):
        co = result

    agent["llm"] = deep_merge(agent.get("llm") or {}, co.get("llm") or {})
    if co.get("auxiliary"):
        agent["auxiliary"] = deep_merge(agent.get("auxiliary") or {}, co["auxiliary"])
    if co.get("env_keys"):
        agent.setdefault("env_keys", {}).update(co["env_keys"])
    return delivered
