"""LiteLLM gateway control plane — orchestrator-side admin client + catalog sync.

The gateway (an in-chart LiteLLM proxy) is the single chokepoint all agent LLM
traffic traverses, so it can *measure* aggregate RPM/TPM and, later, *throttle*
it (see docs/features/usage_monitoring_and_rate_limiting.md). LiteLLM only knows
which upstreams exist if something registers them; on this deployment the model
catalog is admin-curated and lives **encrypted in the app DB**, with no Secret
copy. So the orchestrator owns the sync: it reads the catalog, decrypts each
upstream's key (exactly as it already does at dispatch), and registers the
models into LiteLLM via its admin API.

**Slice 1 scope (this module):**
- Register **endpoint-kind** catalog models only (OpenAI-compatible custom
  endpoints — e.g. the homelab router). `system`-provider models (direct
  OpenAI/Anthropic/Google) stay on their current direct path until Slice 2,
  so the agent's API surface for those is unchanged (avoids the native-Gemini
  function-call-ordering quirks, etc.).
- **No rate limits / no per-job keys** — pure pass-through so the native
  dashboard shows live RPM/TPM. Slice 2 adds `model_rpm_limit` dicts + per-job
  virtual keys on top of this same client.

The sync is **idempotent** and reconciles on a loop: models we own carry a
``model_info.id`` of ``srw-<catalog_uuid>`` plus an ``srw_rev`` stamp derived
from the endpoint's ``updated_at`` (so a key rotation or base-url edit, which
bumps ``updated_at``, forces a re-register without us ever reading the masked
upstream key back out of LiteLLM).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict

import httpx

logger = logging.getLogger(__name__)

# model_info.id prefix marking a deployment this orchestrator owns. The reconcile
# only ever adds/deletes models under this prefix — anything an operator adds by
# hand in the LiteLLM UI is left untouched.
OWNED_ID_PREFIX = "srw-"

# How often the reconcile loop re-syncs the catalog into the gateway. Cheap
# (a handful of models); a LISTEN/NOTIFY trigger can replace polling later.
DEFAULT_SYNC_INTERVAL_S = 60.0


def _rev_for_endpoint(endpoint: Dict[str, Any]) -> int:
    """Revision stamp for an endpoint, from its ``updated_at``.

    Editing an endpoint's base_url or api_key bumps ``updated_at``; folding that
    into the desired ``srw_rev`` lets the reconcile detect the change and
    re-register, even though LiteLLM masks the stored api_key on read-back.
    """
    updated = endpoint.get("updated_at")
    try:
        return int(updated.timestamp()) if updated is not None else 0
    except (AttributeError, ValueError, OSError):
        return 0


class LiteLLMClient:
    """Thin async client for the LiteLLM proxy admin + health API.

    Auth is the proxy master key (Bearer). Methods raise on transport errors so
    the caller (the reconcile loop) can log-and-continue — a gateway blip must
    never take down orchestrator startup.
    """

    def __init__(
        self, base_url: str, master_key: str, *, timeout: float = 15.0
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {master_key}"}
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def is_ready(self) -> bool:
        """True once the proxy answers readiness (DB connected, etc.)."""
        try:
            resp = await self._client.get(
                f"{self._base_url}/health/readiness", headers=self._headers
            )
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def list_managed_models(self) -> Dict[str, Dict[str, Any]]:
        """Return currently-registered models we own, keyed by ``model_info.id``.

        Reads ``GET /model/info`` (api_key is masked in the response — we never
        rely on reading it back). Only entries whose id starts with
        :data:`OWNED_ID_PREFIX` are returned.
        """
        resp = await self._client.get(
            f"{self._base_url}/model/info", headers=self._headers
        )
        resp.raise_for_status()
        out: Dict[str, Dict[str, Any]] = {}
        for entry in resp.json().get("data", []):
            info = entry.get("model_info") or {}
            mid = info.get("id")
            if isinstance(mid, str) and mid.startswith(OWNED_ID_PREFIX):
                out[mid] = {
                    "model_name": entry.get("model_name"),
                    "litellm_params": entry.get("litellm_params") or {},
                    "model_info": info,
                }
        return out

    async def add_model(self, spec: Dict[str, Any]) -> None:
        """Register a deployment (``POST /model/new``)."""
        resp = await self._client.post(
            f"{self._base_url}/model/new", headers=self._headers, json=spec
        )
        resp.raise_for_status()

    async def delete_model(self, model_info_id: str) -> None:
        """Deregister a deployment by its ``model_info.id`` (``POST /model/delete``)."""
        resp = await self._client.post(
            f"{self._base_url}/model/delete",
            headers=self._headers,
            json={"id": model_info_id},
        )
        resp.raise_for_status()


async def build_desired_models(postgres_db: Any) -> Dict[str, Dict[str, Any]]:
    """Compute the LiteLLM deployments the catalog wants, keyed by ``model_info.id``.

    Slice 1: **endpoint-kind, enabled** models only. Each maps to one
    OpenAI-compatible LiteLLM deployment pointing at the endpoint's base_url with
    the decrypted endpoint key. The public ``model_name`` equals the catalog
    ``model_id`` so the agent's existing model string routes unchanged.
    """
    rows = await postgres_db.list_models(provider_kind="endpoint", enabled_only=True)
    if not rows:
        return {}

    # Resolve each distinct endpoint once (decrypted base_url + api_key).
    endpoint_cache: Dict[str, Dict[str, Any] | None] = {}
    desired: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        endpoint_id = row.get("provider_ref")
        model_id = row.get("model_id")
        if not endpoint_id or not model_id:
            continue
        if endpoint_id not in endpoint_cache:
            try:
                endpoint_cache[endpoint_id] = await postgres_db.get_user_llm_endpoint(
                    endpoint_id
                )
            except Exception:
                logger.exception(
                    "LiteLLM sync: failed to resolve endpoint %s for model %s",
                    endpoint_id,
                    model_id,
                )
                endpoint_cache[endpoint_id] = None
        endpoint = endpoint_cache[endpoint_id]
        if not endpoint or not endpoint.get("base_url"):
            logger.warning(
                "LiteLLM sync: skipping %s — endpoint %s has no base_url",
                model_id,
                endpoint_id,
            )
            continue

        owned_id = f"{OWNED_ID_PREFIX}{row['id']}"
        litellm_params: Dict[str, Any] = {
            # openai/ → OpenAI-compatible provider; LiteLLM strips the prefix and
            # sends model=<model_id> to api_base, matching what the agent's own
            # OpenAI factory sends today.
            "model": f"openai/{model_id}",
            "api_base": endpoint["base_url"],
        }
        api_key = endpoint.get("api_key")
        if api_key:
            litellm_params["api_key"] = api_key
        desired[owned_id] = {
            "model_name": model_id,
            "litellm_params": litellm_params,
            "model_info": {
                "id": owned_id,
                "srw_managed": True,
                "srw_rev": _rev_for_endpoint(endpoint),
            },
        }
    return desired


def _needs_replace(current: Dict[str, Any], desired: Dict[str, Any]) -> bool:
    """True if an already-registered deployment drifted from the desired spec.

    Compares the *visible* upstream target (``litellm_params.model`` +
    ``api_base``) and the ``srw_rev`` stamp (catches key rotation, which never
    surfaces in the masked api_key on read-back).
    """
    cur_params = current.get("litellm_params") or {}
    des_params = desired.get("litellm_params") or {}
    if cur_params.get("model") != des_params.get("model"):
        return True
    if cur_params.get("api_base") != des_params.get("api_base"):
        return True
    cur_rev = (current.get("model_info") or {}).get("srw_rev")
    des_rev = (desired.get("model_info") or {}).get("srw_rev")
    # Only treat a *known* rev mismatch as drift — if LiteLLM dropped the custom
    # field on read-back (cur_rev is None), don't thrash on every cycle.
    return cur_rev is not None and cur_rev != des_rev


async def sync_catalog_to_gateway(
    postgres_db: Any, client: LiteLLMClient
) -> Dict[str, int]:
    """Reconcile the gateway's owned deployments to match the catalog.

    Idempotent: adds missing, replaces drifted (delete+add), deletes ours that
    the catalog no longer wants. Returns a small counts dict for logging.
    """
    desired = await build_desired_models(postgres_db)
    current = await client.list_managed_models()

    added = replaced = deleted = 0

    # Add missing + replace drifted.
    for owned_id, spec in desired.items():
        cur = current.get(owned_id)
        if cur is None:
            await client.add_model(spec)
            added += 1
        elif _needs_replace(cur, spec):
            await client.delete_model(owned_id)
            await client.add_model(spec)
            replaced += 1

    # Delete ours that are no longer desired (disabled / removed models).
    for owned_id in current:
        if owned_id not in desired:
            await client.delete_model(owned_id)
            deleted += 1

    if added or replaced or deleted:
        logger.info(
            "LiteLLM catalog sync: +%d ~%d -%d (now %d managed)",
            added,
            replaced,
            deleted,
            len(desired),
        )
    return {
        "added": added,
        "replaced": replaced,
        "deleted": deleted,
        "managed": len(desired),
    }


async def litellm_sync_loop(
    shutdown_event: asyncio.Event,
    postgres_db: Any,
    *,
    interval: float = DEFAULT_SYNC_INTERVAL_S,
) -> None:
    """Background task: keep the gateway's model list in step with the catalog.

    No-op (returns immediately) when ``LITELLM_BASE_URL`` is unset — that env is
    only populated by the chart when ``litellm.enabled``, so this stays dormant
    on deployments without the gateway. Never raises into startup: a gateway
    outage is logged and retried on the next tick.
    """
    base_url = os.getenv("LITELLM_BASE_URL", "").strip()
    master_key = os.getenv("LITELLM_MASTER_KEY", "").strip()
    if not base_url:
        logger.info("LiteLLM gateway sync disabled (LITELLM_BASE_URL unset)")
        return
    if not master_key:
        logger.warning(
            "LiteLLM gateway sync: LITELLM_BASE_URL set but LITELLM_MASTER_KEY "
            "is empty — admin API calls will be rejected. Skipping."
        )
        return

    client = LiteLLMClient(base_url, master_key)
    logger.info("LiteLLM gateway sync starting (gateway=%s)", base_url)
    try:
        while not shutdown_event.is_set():
            try:
                if await client.is_ready():
                    await sync_catalog_to_gateway(postgres_db, client)
                else:
                    logger.debug("LiteLLM gateway not ready yet; will retry")
            except Exception:
                logger.exception("LiteLLM catalog sync failed (non-fatal)")
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
    finally:
        await client.aclose()
        logger.info("LiteLLM gateway sync stopped")
