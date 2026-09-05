"""Idempotent seeder for system-scoped LLM providers, endpoints, and models.

Invoked by the helm post-install/post-upgrade Job as::

    python -m seed.llm_config --payload /seed/llm.yaml

(The orchestrator container's Dockerfile flattens ``orchestrator/`` into
``/app/`` with ``PYTHONPATH=/app``, so the package path is ``seed.*`` at
runtime, not ``orchestrator.seed.*``.)

The payload describes the providers and endpoints the operator wants present on
a fresh stack. On each run:

* ``systemApiKeys`` entries are inserted only for providers that do not yet
  have a row in ``system_api_keys``. Existing rows (whether seeded previously
  or created via Admin → Providers) are never overwritten.
* ``systemEndpoints`` entries are matched by label. Missing endpoints are
  created; existing ones are left alone apart from their model list — any
  listed model that is not already present gets appended.
* ``systemModels`` entries are provider-direct catalog rows
  (``provider_kind='system'``) anchored to a ``system_api_keys`` provider.
  Inserted with ``ON CONFLICT DO NOTHING`` on
  ``(provider_kind, provider_ref, model_id, capability)``, so admin edits
  via the Cockpit survive subsequent helm upgrades. Entries whose provider
  is not (yet) in ``system_api_keys`` are skipped with a log line.

The Job is re-run on every upgrade, so the seeder's success path must be
idempotent. Non-zero exits are reserved for genuine DB errors — a re-run
against an already-seeded stack reports "skipped" for everything and exits 0.

Payload shape::

    systemApiKeys:
      - provider: openai
        apiKeyEnv: "OPENAI_API_KEY"     # resolved from env at run time
        label: "Seeded via helm"
      - provider: anthropic
        apiKey: "sk-ant-..."            # inline plaintext also accepted

    systemEndpoints:
      - label: "Local Gemma"
        baseUrl: "http://vllm.ai.svc.cluster.local:8000/v1"
        apiKeyEnv: "GEMMA_API_KEY"      # optional; omit for keyless endpoints
        models:
          - id: "RedHatAI/gemma-4-31B-it-FP8-Dynamic"
            displayName: "Gemma 4 31B"
            family: "gemma"
            contextWindow: 128000
            reasoningLevel: null
            capability: chat             # optional; defaults to 'chat'
          - id: "qwen3-embedding-8b"
            displayName: "Qwen3 Embedding 8B"
            capability: embedding        # routes to Admin → Defaults → Embedding

    systemModels:
      - provider: "anthropic"
        id: "claude-opus-4-7"
        displayName: "Claude Opus 4.7"
        capability: chat
        family: "claude-opus"
      - provider: "openai"
        id: "text-embedding-3-large"
        displayName: "OpenAI Embedding (Large)"
        capability: embedding

``apiKeyEnv`` lets helm keep the payload ConfigMap plaintext-free: the Job pod
mounts the referenced Secret via ``envFrom`` and the seeder resolves the
variable at run time. If the env var is unset or empty, the entry is skipped
with a warning — a missing key is not fatal, since subsequent entries may
still seed successfully.

Plaintext keys live in the payload only for the lifetime of the Job pod;
the seeder encrypts on write via ``orchestrator.security.crypto.encrypt`` so
they land in Postgres as ``v1:...`` ciphertexts.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from orchestrator.database.postgres import PostgresDB

logger = logging.getLogger("orchestrator.seed.llm_config")

SEEDED_FROM_TAG = "helm:llm.seed"

# Well-known label for the codex-proxy system endpoint. Anything that wants
# to locate the row (admin availability probe, runtime resolver, init seeder)
# matches on this exact string.
CODEX_PROXY_ENDPOINT_LABEL = "codex-proxy"

# ElevenLabs TTS provider, auto-wired from the deployment-wide ELEVENLABS_API_KEY
# secret (see knowledge-base/knowledge/features/tts_vendor_providers.md). Like the codex proxy, the
# key in the secret is all it takes — no manual Admin step.
ELEVENLABS_ENDPOINT_LABEL = "ElevenLabs"
ELEVENLABS_TTS_MODEL_ID = "eleven_multilingual_v2"
# Placeholder base_url: ElevenLabs is NOT OpenAI-compatible, so the TTS adapter
# (services/tts.py) targets ElevenLabs' fixed API URL and ignores this. The
# endpoint row exists only to anchor the catalog model to a transport.
_ELEVENLABS_BASE_URL = "https://api.elevenlabs.io"
# Sarah — one of the standard "premade" voices ElevenLabs ships in every
# account's voice list, so read-aloud works out of the box, including on the
# free tier. (The legacy default, Rachel `21m00Tcm4TlvDq8ikWAM`, was moved to
# the shared Voice Library, which free-tier keys cannot synthesize via the API —
# it 402s with "Free users cannot use library voices".) Users override this via
# the Settings voice picker (default_tts_voice) or the account-voice picker
# (Phase 5).
ELEVENLABS_DEFAULT_VOICE = "EXAVITQu4vr4xnSDxMaL"

# Tavily's deployment secret is a seed input only. The endpoint stores the
# encrypted credential so dispatch can deliver it per job/session.
TAVILY_ENDPOINT_LABEL = "Tavily"
TAVILY_MODEL_ID = "tavily"
_TAVILY_BASE_URL = "https://api.tavily.com"

# Bundled, keyless search service. The Helm seed Job supplies the Service URL;
# the orchestrator/agent runtime never guesses whether the component exists.
SEARXNG_ENDPOINT_LABEL = "SearXNG"
SEARXNG_MODEL_ID = "searxng"

# Fallback used when CODEX_PROXY_URL is unset. Mirrors the runtime fallback
# in ``orchestrator.main._get_codex_subscription_models`` so login flows that
# work without the env var also wire up a transport row.
_DEFAULT_CODEX_PROXY_URL = "http://localhost:8317"


def _resolve_secret_value(entry: dict[str, Any], *, context: str) -> str | None:
    """Resolve an ``apiKey`` from an inline string or an ``apiKeyEnv`` reference.

    Returns None when neither field is set or the env var is empty. The
    caller decides whether that is fatal (system api key) or benign (optional
    endpoint key).
    """
    inline = entry.get("apiKey") or entry.get("api_key")
    if inline:
        return inline

    env_name = entry.get("apiKeyEnv") or entry.get("api_key_env")
    if env_name:
        value = os.environ.get(env_name)
        if not value:
            logger.warning(
                "%s: env var %s is unset or empty — secret not resolved",
                context,
                env_name,
            )
            return None
        return value

    return None


_CAPABILITY_ENUM = (
    "chat",
    "auxiliary",
    "embedding",
    "vision",
    "whisper",
    "tts",
    "search",
    "fetch",
)


def _resolve_capabilities_from_entry(
    entry: dict[str, Any], *, context: str
) -> list[str] | None:
    """Build the canonical ``capabilities[]`` for a helm seed entry.

    Operator semantics (in order of precedence):

    - ``capabilities: [chat, vision]`` — explicit array, respected as-is.
      No auto-expansion: if you write ``[chat]`` you get a chat-only row.
    - ``capability: chat`` — singular shorthand for the legacy spelling,
      auto-expanded to ``[chat, auxiliary]``. Reflects the design invariant
      that a chat-capable LLM always works for the auxiliary observer/
      curator workload (see src/orchestrator/services/readiness.py:16-21).
    - ``capability: <other>`` — singular for any other enum value lands as
      ``[<other>]``. No expansion (only chat is fungible by default).
    - ``multimodal: true`` — convenience hint that adds ``'vision'`` to a
      chat-capable row regardless of which spelling produced it. Lets
      operators flag known multimodal models (gpt-4o, gemini-2-pro,
      claude-opus-4) without writing the array form.

    Returns ``None`` when the resulting set contains a value outside the
    catalog enum — caller treats that as "skip this entry".
    """
    explicit_caps = entry.get("capabilities")
    if isinstance(explicit_caps, list) and explicit_caps:
        caps = [str(c).lower() for c in explicit_caps]
    else:
        single = str(entry.get("capability") or "chat").lower()
        caps = ["chat", "auxiliary"] if single == "chat" else [single]

    for c in caps:
        if c not in _CAPABILITY_ENUM:
            logger.info(
                "skipping %s — capability %r is not in catalog enum", context, c
            )
            return None

    if entry.get("multimodal") and "chat" in caps and "vision" not in caps:
        caps.append("vision")

    seen: set[str] = set()
    out: list[str] = []
    for c in caps:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


@dataclass
class SeedReport:
    """Outcome summary for a single seed run."""

    api_keys_seeded: list[str] = field(default_factory=list)
    api_keys_skipped: list[str] = field(default_factory=list)
    endpoints_seeded: list[str] = field(default_factory=list)
    endpoints_skipped: list[str] = field(default_factory=list)
    models_seeded: list[tuple[str, str]] = field(default_factory=list)
    models_skipped: list[tuple[str, str]] = field(default_factory=list)

    def log(self) -> None:
        logger.info(
            "seed summary — keys seeded=%d skipped=%d, endpoints seeded=%d "
            "skipped=%d, models seeded=%d skipped=%d",
            len(self.api_keys_seeded),
            len(self.api_keys_skipped),
            len(self.endpoints_seeded),
            len(self.endpoints_skipped),
            len(self.models_seeded),
            len(self.models_skipped),
        )


def load_payload(path: Path) -> dict[str, Any]:
    """Load and lightly validate a seed YAML payload.

    Returns an empty dict when the file is missing or contains an empty
    document — "no seed input" is a valid configuration, not an error.
    """
    if not path.exists():
        logger.info("seed payload %s not found — nothing to seed", path)
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(
            f"seed payload must be a mapping at the top level, got {type(data).__name__}"
        )
    return data


async def _seed_api_keys(
    db: PostgresDB, entries: Iterable[dict[str, Any]], report: SeedReport
) -> None:
    existing = {row["provider"] for row in await db.list_system_api_keys()}
    for entry in entries:
        provider = entry.get("provider")
        if not provider:
            logger.warning("skipping systemApiKeys entry without provider: %r", entry)
            continue
        api_key = _resolve_secret_value(entry, context=f"systemApiKeys[{provider}]")
        if not api_key:
            logger.warning(
                "skipping systemApiKeys[%s] — no apiKey / apiKeyEnv resolved",
                provider,
            )
            continue
        if provider in existing:
            report.api_keys_skipped.append(provider)
            logger.info("api key for %s already present — skipped", provider)
            continue

        label = entry.get("label")
        await db.upsert_system_api_key(
            provider=provider,
            api_key=api_key,
            key_prefix=api_key[:8],
            label=label,
            seeded_from=SEEDED_FROM_TAG,
        )
        report.api_keys_seeded.append(provider)
        logger.info("seeded system api key for %s", provider)


async def _seed_endpoints(
    db: PostgresDB, entries: Iterable[dict[str, Any]], report: SeedReport
) -> None:
    existing_rows = await db.list_system_llm_endpoints()
    by_label = {row["label"]: row for row in existing_rows}

    for entry in entries:
        label = entry.get("label")
        base_url = entry.get("baseUrl") or entry.get("base_url")
        if not label or not base_url:
            logger.warning(
                "skipping systemEndpoints entry — label or baseUrl missing: %r", entry
            )
            continue

        models = entry.get("models") or []
        if not isinstance(models, list):
            logger.warning(
                "skipping systemEndpoints[%s] — models must be a list", label
            )
            continue

        existing = by_label.get(label)
        if existing is None:
            api_key = _resolve_secret_value(entry, context=f"systemEndpoints[{label}]")
            created = await db.create_system_llm_endpoint(
                label=label,
                base_url=base_url,
                api_key=api_key,
                key_prefix=(api_key[:8] if api_key else None),
            )
            endpoint_id = str(created["id"])
            report.endpoints_seeded.append(label)
            logger.info("seeded system endpoint %s (%s)", label, base_url)
        else:
            endpoint_id = str(existing["id"])
            report.endpoints_skipped.append(label)
            logger.info("endpoint %s already present — leaving untouched", label)

        # Per-endpoint model entries become catalog rows with
        # provider_kind='endpoint'. Capabilities outside the catalog enum
        # (whisper, tts) are skipped — those don't surface in v1.
        from shared.runtime.core.model_registry import (
            family_of,
        )  # local: src/* lazy load

        # Aggregate per-endpoint duplicates: same model_id appearing under
        # multiple capabilities collapses into one row whose capabilities[]
        # is the union. Same admin-edit-safety semantics as
        # _seed_system_models — first occurrence's metadata wins.
        endpoint_aggregated: dict[str, dict[str, Any]] = {}
        for model in models:
            model_id = model.get("id") or model.get("model_id")
            if not model_id:
                logger.warning(
                    "skipping model entry under %s — id missing: %r", label, model
                )
                continue
            capabilities = _resolve_capabilities_from_entry(
                model, context=f"systemEndpoints[{label}].models[{model_id}]"
            )
            if capabilities is None:
                continue
            existing = endpoint_aggregated.get(model_id)
            if existing is None:
                endpoint_aggregated[model_id] = {
                    "model": model,
                    "capabilities": list(capabilities),
                }
            else:
                existing_caps = existing["capabilities"]
                for c in capabilities:
                    if c not in existing_caps:
                        existing_caps.append(c)

        for model_id, agg in endpoint_aggregated.items():
            model = agg["model"]
            capabilities = agg["capabilities"]
            display_label = (
                model.get("displayName") or model.get("display_name") or model_id
            )
            inserted = await db.create_model(
                provider_kind="endpoint",
                provider_ref=endpoint_id,
                model_id=model_id,
                display_label=display_label,
                capabilities=capabilities,
                family=model.get("family") or family_of(model_id),
                context_window=model.get("contextWindow")
                or model.get("context_window"),
                reasoning_level=model.get("reasoningLevel")
                or model.get("reasoning_level"),
                enabled=model.get("enabled", True),
                seeded_from="helm:llm.seed",
                on_conflict_do_nothing=True,
            )
            if inserted is None:
                report.models_skipped.append((label, model_id))
                continue
            report.models_seeded.append((label, model_id))
            logger.info(
                "seeded catalog row %s (capabilities=%s) under endpoint %s",
                model_id,
                capabilities,
                label,
            )


async def _seed_system_models(
    db: PostgresDB, entries: Iterable[dict[str, Any]], report: SeedReport
) -> None:
    """Seed provider-direct catalog rows (provider_kind='system').

    Each entry is anchored to a ``system_api_keys`` provider that must
    already exist. Entries whose provider has no key are skipped — same
    contract as the legacy ``_seed_models_from_yaml`` path this replaces.

    Aggregation: under the array-capability model, multiple helm entries
    pointing at the same (provider, model_id) get merged into ONE row
    whose capabilities[] is the union of the contributions. This handles
    legacy helm shapes where operators wrote one entry per capability for
    the same physical model (gpt-4o + capability: chat alongside
    gpt-4o + capability: vision). After aggregation we issue a single
    INSERT per (provider, model_id) — preserving the original admin-edit
    safety: ON CONFLICT DO NOTHING leaves admin-modified rows alone.
    """
    seeded_providers = {k["provider"] for k in await db.list_system_api_keys()}

    from shared.runtime.core.model_registry import family_of  # local: src/* lazy load

    aggregated: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries:
        provider = entry.get("provider")
        model_id = entry.get("id") or entry.get("model_id")
        if not provider or not model_id:
            logger.warning(
                "skipping systemModels entry — provider or id missing: %r", entry
            )
            continue

        if provider not in seeded_providers:
            logger.info(
                "skipping systemModels[%s/%s] — no system_api_keys row for "
                "provider yet (seed the key first or add via Admin → Providers)",
                provider,
                model_id,
            )
            report.models_skipped.append((provider, model_id))
            continue

        capabilities = _resolve_capabilities_from_entry(
            entry, context=f"systemModels[{provider}/{model_id}]"
        )
        if capabilities is None:
            continue

        key = (provider, model_id)
        existing = aggregated.get(key)
        if existing is None:
            aggregated[key] = {
                "entry": entry,
                "capabilities": list(capabilities),
            }
        else:
            # Union the capabilities (preserve order, dedupe). Other metadata
            # comes from the FIRST entry seen — operators who care about
            # display_label/family disambiguation should put their preferred
            # entry first in the helm values.
            existing_caps = existing["capabilities"]
            for c in capabilities:
                if c not in existing_caps:
                    existing_caps.append(c)

    for (provider, model_id), agg in aggregated.items():
        entry = agg["entry"]
        capabilities = agg["capabilities"]
        display_label = (
            entry.get("displayName") or entry.get("display_name") or model_id
        )
        inserted = await db.create_model(
            provider_kind="system",
            provider_ref=provider,
            model_id=model_id,
            display_label=display_label,
            capabilities=capabilities,
            family=entry.get("family") or family_of(model_id),
            context_window=entry.get("contextWindow") or entry.get("context_window"),
            reasoning_level=entry.get("reasoningLevel") or entry.get("reasoning_level"),
            enabled=entry.get("enabled", True),
            seeded_from=SEEDED_FROM_TAG,
            on_conflict_do_nothing=True,
        )
        if inserted is None:
            report.models_skipped.append((provider, model_id))
            continue
        report.models_seeded.append((provider, model_id))
        logger.info(
            "seeded catalog row %s (capabilities=%s) under system provider %s",
            model_id,
            capabilities,
            provider,
        )


async def seed(db: PostgresDB, payload: dict[str, Any]) -> SeedReport:
    """Apply the seed payload against an already-connected ``PostgresDB``."""
    report = SeedReport()
    api_keys = payload.get("systemApiKeys") or []
    endpoints = payload.get("systemEndpoints") or []
    system_models = payload.get("systemModels") or []

    if (
        not isinstance(api_keys, list)
        or not isinstance(endpoints, list)
        or not isinstance(system_models, list)
    ):
        raise ValueError(
            "systemApiKeys, systemEndpoints, and systemModels must be lists when present"
        )

    if api_keys:
        await _seed_api_keys(db, api_keys, report)
    if endpoints:
        await _seed_endpoints(db, endpoints, report)
    if system_models:
        await _seed_system_models(db, system_models, report)
    return report


async def ensure_codex_proxy_endpoint(
    db: PostgresDB, *, proxy_url: str | None = None
) -> bool:
    """Ensure a system-scoped ``codex-proxy`` row exists in ``llm_endpoints``.

    Called from runtime paths (the OAuth callback, the admin availability
    probe) so that connecting a ChatGPT subscription via the cockpit wires
    the proxy as a provider without requiring an init re-run or the
    ``CODEX_PROXY_URL`` env var to be set. Idempotent: re-runs short-circuit
    on label match inside :func:`seed`.

    Returns True if a new row was created, False otherwise (already
    present, or the seed run failed). Failures are logged but never raised
    — callers should never 500 on a transport-row wiring hiccup.
    """
    url = proxy_url or os.environ.get("CODEX_PROXY_URL") or _DEFAULT_CODEX_PROXY_URL
    base_url = url.rstrip("/")
    if not base_url.endswith("/v1"):
        base_url = f"{base_url}/v1"

    payload = {
        "systemEndpoints": [
            {
                "label": CODEX_PROXY_ENDPOINT_LABEL,
                "baseUrl": base_url,
                "apiKeyEnv": "CODEX_MANAGEMENT_KEY",
                "models": [],
            }
        ]
    }
    try:
        report = await seed(db, payload)
    except Exception:
        logger.exception("ensure_codex_proxy_endpoint: seed run failed")
        return False
    return CODEX_PROXY_ENDPOINT_LABEL in report.endpoints_seeded


async def ensure_elevenlabs_tts_endpoint(db: PostgresDB) -> bool:
    """Ensure the ElevenLabs TTS model is registered when ``ELEVENLABS_API_KEY``
    is set — the read-aloud provider then appears in the picker with no manual
    Admin step, exactly like the codex proxy.

    Design (knowledge-base/knowledge/features/tts_vendor_providers.md): the env secret is the single
    source of truth for the key. The endpoint row is stored **without** a key
    (``api_key=None``) purely to anchor the catalog model; the TTS adapter reads
    ``ELEVENLABS_API_KEY`` from the environment at synth time, so rotating the
    secret takes effect with no DB write. The catalog row's
    ``params_json.provider`` routes synthesis to the ElevenLabs adapter (the
    model-id sniff would too) and seeds a default voice so playback works out of
    the box.

    Idempotent — a re-run finds the existing endpoint by label and the model row
    is inserted ``ON CONFLICT DO NOTHING`` (admin edits survive). Best-effort:
    failures are logged, never raised, so a wiring hiccup can't abort startup.

    Returns True if a new model row was created.
    """
    if not os.environ.get("ELEVENLABS_API_KEY"):
        return False
    try:
        endpoint_id: str | None = None
        for row in await db.list_system_llm_endpoints():
            if row.get("label") == ELEVENLABS_ENDPOINT_LABEL:
                endpoint_id = str(row["id"])
                break
        if endpoint_id is None:
            created = await db.create_system_llm_endpoint(
                label=ELEVENLABS_ENDPOINT_LABEL,
                base_url=_ELEVENLABS_BASE_URL,
                api_key=None,  # env is the source of truth; adapter reads it
                key_prefix=None,
            )
            endpoint_id = str(created["id"])
        inserted = await db.create_model(
            provider_kind="endpoint",
            provider_ref=endpoint_id,
            model_id=ELEVENLABS_TTS_MODEL_ID,
            display_label="ElevenLabs Multilingual v2",
            capabilities=["tts"],
            family="elevenlabs",
            params_json={
                "provider": "elevenlabs",
                "voice": ELEVENLABS_DEFAULT_VOICE,
            },
            enabled=True,
            seeded_from="env:ELEVENLABS_API_KEY",
            on_conflict_do_nothing=True,
        )
        if inserted is not None:
            logger.info(
                "ensure_elevenlabs_tts_endpoint: registered %s under endpoint %s",
                ELEVENLABS_TTS_MODEL_ID,
                endpoint_id,
            )
        return inserted is not None
    except Exception:
        logger.exception("ensure_elevenlabs_tts_endpoint: wiring failed")
        return False


async def ensure_tavily_search_endpoint(db: PostgresDB) -> bool:
    """Convert a legacy ``TAVILY_API_KEY`` into catalog-backed web providers.

    The seed is deliberately one-shot. Any existing search row means an admin
    already made a choice. An existing well-known endpoint with no model is a
    tombstone left by an admin-deleted catalog row and is not recreated.
    Defaults are filled only when empty, so no operator selection is clobbered.

    Returns True only when a new Tavily catalog row was inserted. Failures are
    best-effort and never abort orchestrator startup.
    """

    api_key = (os.environ.get("TAVILY_API_KEY") or "").strip()
    if not api_key:
        return False
    try:
        if await db.list_models(capabilities=["search"]):
            return False

        for endpoint in await db.list_system_llm_endpoints():
            if endpoint.get("label") == TAVILY_ENDPOINT_LABEL:
                return False

        endpoint = await db.create_system_llm_endpoint(
            label=TAVILY_ENDPOINT_LABEL,
            base_url=_TAVILY_BASE_URL,
            api_key=api_key,
            key_prefix=api_key[:8],
        )
        inserted = await db.create_model(
            provider_kind="endpoint",
            provider_ref=str(endpoint["id"]),
            model_id=TAVILY_MODEL_ID,
            display_label="Tavily",
            capabilities=["search", "fetch"],
            family="tavily",
            params_json={
                "provider": "tavily",
                "ops": ["search", "extract", "crawl", "map"],
            },
            enabled=True,
            seeded_from="env:TAVILY_API_KEY",
            on_conflict_do_nothing=True,
        )
        if inserted is None:
            return False

        for capability in ("search", "fetch"):
            if not await db.get_default_llm_model(capability):
                await db.set_default_llm_model(capability, TAVILY_MODEL_ID)
        logger.info(
            "ensure_tavily_search_endpoint: registered Tavily search/fetch provider"
        )
        return True
    except Exception:
        logger.exception("ensure_tavily_search_endpoint: wiring failed")
        return False


async def ensure_searxng_search_endpoint(
    db: PostgresDB, *, base_url: str | None = None
) -> bool:
    """Register the bundled SearXNG service and fill an empty search slot.

    This runs after :func:`ensure_tavily_search_endpoint`. A fresh install gets
    SearXNG as its primary search provider; an install whose primary is already
    Tavily (or an admin-selected provider) gets SearXNG as the fallback. The
    default writes happen only alongside the first catalog-row insert, so an
    admin can subsequently clear or replace either slot without a later boot
    undoing that choice.

    An existing well-known endpoint without its model is an admin-deletion
    tombstone and is not repaired. Returns True only when a new catalog row was
    inserted. Failures are best-effort and never abort startup.
    """

    url = (base_url or os.environ.get("SEARXNG_BASE_URL") or "").strip().rstrip("/")
    if not url:
        return False
    try:
        for endpoint in await db.list_system_llm_endpoints():
            if endpoint.get("label") == SEARXNG_ENDPOINT_LABEL:
                return False

        endpoint = await db.create_system_llm_endpoint(
            label=SEARXNG_ENDPOINT_LABEL,
            base_url=url,
            api_key=None,
            key_prefix=None,
        )
        inserted = await db.create_model(
            provider_kind="endpoint",
            provider_ref=str(endpoint["id"]),
            model_id=SEARXNG_MODEL_ID,
            display_label="SearXNG (self-hosted)",
            capabilities=["search"],
            family="searxng",
            params_json={"provider": "searxng", "ops": ["search"]},
            enabled=True,
            seeded_from="helm:searxng",
            on_conflict_do_nothing=True,
        )
        if inserted is None:
            return False

        primary = await db.get_default_llm_model("search")
        if not primary:
            await db.set_default_llm_model("search", SEARXNG_MODEL_ID)
        elif primary != SEARXNG_MODEL_ID and not await db.get_default_llm_model(
            "search_fallback"
        ):
            await db.set_default_llm_model("search_fallback", SEARXNG_MODEL_ID)
        logger.info(
            "ensure_searxng_search_endpoint: registered bundled SearXNG provider"
        )
        return True
    except Exception:
        logger.exception("ensure_searxng_search_endpoint: wiring failed")
        return False


async def run(payload_path: Path) -> SeedReport:
    """Open a DB connection, apply the seed, and report."""
    payload = load_payload(payload_path)
    if not payload:
        report = SeedReport()
        report.log()
        return report

    db = PostgresDB()
    await db.connect()
    try:
        report = await seed(db, payload)
    finally:
        await db.close()
    report.log()
    return report


async def run_research_provider_seed() -> None:
    """Run deployment-provided research seeders in their required order."""

    db = PostgresDB()
    await db.connect()
    try:
        # The legacy key must claim an empty primary before bundled SearXNG is
        # allowed to fill either slot.
        await ensure_tavily_search_endpoint(db)
        await ensure_searxng_search_endpoint(db)
    finally:
        await db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--payload",
        type=Path,
        default=Path("/seed/llm.yaml"),
        help="Path to the seed YAML payload (default: /seed/llm.yaml).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Python log level name (default: INFO).",
    )
    parser.add_argument(
        "--research-providers-only",
        action="store_true",
        help="Run only the Tavily/SearXNG boot seeders (no payload required).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        if args.research_providers_only:
            asyncio.run(run_research_provider_seed())
        else:
            asyncio.run(run(args.payload))
    except Exception:
        logger.exception("seed run failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
