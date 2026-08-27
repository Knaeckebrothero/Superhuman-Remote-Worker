"""Single source of agent config resolution (supersedes agent-side Decision 6).

Composes ``src/core/loader`` steps so the orchestrator produces the full, frozen
config blob the agent hydrates. Reused by job dispatch AND session attach —
identical resolution; only timing/delivery differ. See
``knowledge-base/knowledge/superpowers/specs/2026-06-17-orchestrator-resolved-config-design.md``.

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
from src.core.tool_policy import normalize_tool_policy

# Prompt segments a DB/forked expert may override — one family-agnostic version
# each (model adaptation stays in the systemprompt_<family> wrapper). persona +
# instructions are v1 (migration 0028); strategic/tactical/summarization are
# Part 2 (full DB-expert prompt parity). Reused to build the ``_db_prompt_keys``
# provenance marker the render path fences on.
_OVERLAY_PROMPT_KEYS = (
    "persona",
    "instructions",
    "strategic",
    "tactical",
    "summarization",
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
    skills: Optional[dict] = None,
    grant_strip: Optional[Callable[[dict], dict]] = None,
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

    ``grant_strip``, when given, runs on the fully-merged ``data`` before BOTH
    ``capture`` and the returned blob are built from it — so the PDP's capture
    and the blob the agent hydrates always agree. Applying it only to
    ``capture`` would silence the dispatch check while still delivering the
    stripped capability.
    """
    base_path, deployment_dir = resolve_config_path(base_config_name)

    # A named bundled expert is logically an expert overlay, not the base layer.
    # Split its leaf from $extends so account fallbacks can sit above the real
    # framework base but below the bundled expert, matching DB expert precedence.
    # (Previously account defaults silently replaced bundled expert models.)
    bundled_leaf: dict = {}
    parent_path: str | None = None
    try:
        with open(base_path, "r", encoding="utf-8") as f:
            raw_leaf = yaml.safe_load(f) or {}
        if isinstance(raw_leaf, dict) and raw_leaf.get("$extends"):
            parent_path, _ = resolve_config_path(str(raw_leaf["$extends"]))
            bundled_leaf = dict(raw_leaf)
            bundled_leaf.pop("$extends", None)
            # Read straight off disk, so it bypasses load_and_merge_config's
            # normalisation and needs its own.
            bundled_leaf = normalize_tool_policy(bundled_leaf)
    except Exception:
        raw_leaf = {}

    if parent_path:
        data = load_and_merge_config(parent_path)
        explicit_llm_keys = _raw_leaf_llm_keys(parent_path)
    else:
        data = load_and_merge_config(base_path)
        explicit_llm_keys = _raw_leaf_llm_keys(base_path)

    # Default-model floor: replace the base placeholder model before the expert
    # merges (the expert/request still override it).
    if base_defaults:
        if base_defaults.get("llm"):
            explicit_llm_keys |= set(base_defaults["llm"].keys())
        data = deep_merge(data, normalize_tool_policy(base_defaults))

    if bundled_leaf:
        if bundled_leaf.get("llm"):
            explicit_llm_keys |= set(bundled_leaf["llm"].keys())
        data = deep_merge(data, bundled_leaf)

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
        # Part 2: record which prompt segments are DB-authored (untrusted) so the
        # render path fences strategic/tactical (fence_phase_directive) and brace-
        # escapes summarization. Only the segments actually present in the row —
        # inherited (disk) segments stay trusted.
        data["_db_prompt_keys"] = [
            k for k in _OVERLAY_PROMPT_KEYS if prompts_override.get(k)
        ]

    # Every remaining authored layer, normalised at birth. Layer-local
    # expansion is what lets deep_merge stay untouched: each layer is already
    # list[str] by the time it merges, so the existing "most specific layer
    # that mentions a category wins it wholesale" rule carries unchanged.
    # Server-generated request fragments (_critic_config_override,
    # the campaign-loop {"loop": ["loop_plan"]}) ride this same path and need
    # no special handling.
    for layer in (project_overrides, db_overrides, user_settings, request_override):
        if layer:
            if layer.get("llm"):
                explicit_llm_keys |= set(layer["llm"].keys())
            data = deep_merge(data, normalize_tool_policy(layer))

    _apply_settings_matrix(data, explicit_llm_keys, deployment_dir)

    # Idempotent sweep, and it must land HERE — before the capture, not inside
    # load_agent_config_from_dict. capture["merged_fragment"] is what the
    # dispatch PDP evaluates, and capability_grants._truthy is wrong on two raw
    # policy shapes: it reads {} as false (grant violation missed) and
    # {only: []} as true (violation fabricated for an empty group). Both are
    # refused by the normaliser, and the PDP only ever sees lists.
    data = normalize_tool_policy(data)

    # Applied HERE so the capture the PDP evaluates and the blob the agent
    # hydrates are the same stripped config. Stripping only the capture would
    # silence the check while still delivering the capability.
    if grant_strip is not None:
        data = grant_strip(data)

    if capture is not None:
        # Full merged config in fragment shape — the policy view for the dispatch
        # PEP (single PDP). The base's shell/delegation are present here; deny-by-
        # default is reconciled by grandfathering existing users (migration 0030).
        capture["merged_fragment"] = copy.deepcopy(data)

    config = load_agent_config_from_dict(data, deployment_dir=deployment_dir)
    blob = serialize_resolved_config(config, model=config.llm.model)

    # Overlay the DB expert's prompt segments onto the resolved prompts so the
    # frozen blob matches what the render path emits: serialize_resolved_config
    # only sees disk + config, never the expert row's out-of-band prompts. The
    # untrusted segments are fenced at render via the _persona_source /
    # _db_prompt_keys markers (decision 7 + Part 2).
    for _k in _OVERLAY_PROMPT_KEYS:
        if prompts_override.get(_k):
            blob["prompts"][_k] = prompts_override[_k]

    # Slice-2 skills runtime: attach the pre-gathered in-scope skill menu + file
    # trees. DB I/O happens in the caller (orchestrator/main.py); this keeps the
    # blob shape identical for jobs and sessions and unit-testable without a DB.
    if skills:
        blob["skills"] = skills

    return blob


def unrouted_model_slots(blob: dict) -> list[str]:
    """Return descriptions of model-bearing slots in a *credentialed* delivery
    blob that have NO transport — no ``base_url``, ``api_key``, or ``provider``.

    Such a slot has no way to reach its endpoint: the agent's ``create_llm``
    falls back to the OpenAI factory default (``api.openai.com``) and the model
    almost certainly 401/404s with an opaque error (incident: job eec20eeb —
    codex phase pins shipped without transport). Dispatch uses this to **fail
    fast** with an actionable message instead of letting the agent misroute.

    Conservative by design: a slot is only flagged when it carries a ``model``
    yet none of the three routing fields. A model with a ``provider`` (factory
    default base URL) or an inline ``api_key`` is considered routable.
    """
    agent = blob.get("agent") or {}
    llm = agent.get("llm") or {}
    problems: list[str] = []

    def _check(section: object, label: str) -> None:
        if not isinstance(section, dict):
            return
        model = section.get("model")
        if not model:
            return
        if not (
            section.get("base_url") or section.get("api_key") or section.get("provider")
        ):
            problems.append(f"{label} model {model!r}")

    _check(llm, "llm")
    if isinstance(llm, dict):
        for _phase in ("strategic", "tactical"):
            _check(llm.get(_phase), f"llm.{_phase}")
    _check(agent.get("auxiliary"), "auxiliary")
    return problems


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
    #
    # This MUST reach the nested phase blocks (llm.strategic / llm.tactical /
    # llm.summarization), not just the top-level section. serialize_resolved_config
    # emits those with explicit base_url=None / provider=None leaves; a
    # present-but-None key defeats the injector's setdefault, so endpoint-backed
    # phase pins (e.g. codex models) shipped without transport and 401'd against
    # api.openai.com while the base model — whose top-level None WAS stripped —
    # worked, masking the gap (incident: job eec20eeb / "Research 01").
    def _strip_none_keys(d: dict) -> None:
        for _k in [_k for _k, _v in d.items() if _v is None]:
            del d[_k]

    for _sect in ("llm", "auxiliary"):
        _s = co.get(_sect)
        if isinstance(_s, dict):
            _strip_none_keys(_s)
            for _sub in _s.values():
                if isinstance(_sub, dict):
                    _strip_none_keys(_sub)

    result = await injector(co)
    if isinstance(result, dict):
        co = result

    agent["llm"] = deep_merge(agent.get("llm") or {}, co.get("llm") or {})
    if co.get("auxiliary"):
        agent["auxiliary"] = deep_merge(agent.get("auxiliary") or {}, co["auxiliary"])
    if co.get("env_keys"):
        agent.setdefault("env_keys", {}).update(co["env_keys"])
    return delivered
