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

try:
    # Host-side: invoked from repo root (e.g. `python init.py`) where the
    # orchestrator package is importable via its full path.
    from orchestrator.database.postgres import PostgresDB
except ImportError:
    # In-container: Dockerfile.orchestrator copies orchestrator/ flat into
    # /app with PYTHONPATH=/app, so the same module is reachable as a
    # top-level `database` package.
    from database.postgres import PostgresDB

logger = logging.getLogger("orchestrator.seed.llm_config")

SEEDED_FROM_TAG = "helm:llm.seed"

# Well-known label for the codex-proxy system endpoint. Anything that wants
# to locate the row (admin availability probe, runtime resolver, init seeder)
# matches on this exact string.
CODEX_PROXY_ENDPOINT_LABEL = "codex-proxy"

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


_CAPABILITY_ENUM = ("chat", "auxiliary", "embedding", "vision", "whisper", "tts")


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
      curator workload (see orchestrator/services/readiness.py:16-21).
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
        from src.core.model_registry import family_of  # local: src/* lazy load

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

    from src.core.model_registry import family_of  # local: src/* lazy load

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
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        asyncio.run(run(args.payload))
    except Exception:
        logger.exception("seed run failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
