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
import hashlib
import hmac
import json
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

# Shared "fleet" virtual key (Slice 2a backstop). All agent traffic routes
# through this one non-admin key instead of the admin master key, so its
# per-model rpm buckets cap the *fleet aggregate* against the upstream — a
# breach returns 429 + Retry-After, which the agent's existing backoff honors.
# (The admin master key bypasses limits, so it can't be the agent credential.)
FLEET_KEY_ALIAS = "srw-fleet-backstop"

# Set True once the fleet key is provisioned in LiteLLM. Until then,
# get_fleet_key() returns None and routing falls back to the master key.
_fleet_key_ready = False
_fleet_spec_hash: str | None = None

# Slice 2b: per-(user, project) scoped keys. Maps (user_id, project_id) → the
# spec hash we last ensured in the gateway, so the hot dispatch path skips the
# team/user/key upserts unless the resolved limits/models actually changed. The
# key *value* is deterministic (compute_scoped_key), so nothing is persisted —
# this cache is a pure latency optimization, safe to lose on restart.
_scoped_ensured: Dict[tuple[str, str], str] = {}


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

    async def upsert_key(self, key: str, *, alias: str, spec: Dict[str, Any]) -> None:
        """Create the key with a fixed value, or update it if it already exists.

        The value is derived deterministically from the master key, so on restart
        the same value is re-asserted (LiteLLM persists keys in its DB) — a
        duplicate ``/key/generate`` falls through to ``/key/update`` so limit
        changes apply without re-minting (and routing never needs the value
        persisted anywhere).
        """
        resp = await self._client.post(
            f"{self._base_url}/key/generate",
            headers=self._headers,
            json={"key": key, "key_alias": alias, **spec},
        )
        if resp.status_code == 200:
            return
        # Already exists (or generate rejected the duplicate) → update in place.
        upd = await self._client.post(
            f"{self._base_url}/key/update",
            headers=self._headers,
            json={"key": key, **spec},
        )
        upd.raise_for_status()

    async def upsert_team(
        self, team_id: str, *, alias: str, spec: Dict[str, Any]
    ) -> None:
        """Create a team with a fixed ``team_id``, or update it (Slice 2b project).

        The team carries the project's per-model ``model_rpm_limit`` dict, which
        LiteLLM enforces **aggregated across every key in the team** (in-memory,
        no Redis) — verified 2026-06-22. ``team_id`` is deterministic
        (``srw-proj-<uuid>``) so a re-create on restart re-asserts the same team.
        """
        resp = await self._client.post(
            f"{self._base_url}/team/new",
            headers=self._headers,
            json={"team_id": team_id, "team_alias": alias, **spec},
        )
        if resp.status_code == 200:
            return
        upd = await self._client.post(
            f"{self._base_url}/team/update",
            headers=self._headers,
            json={"team_id": team_id, **spec},
        )
        upd.raise_for_status()

    async def upsert_internal_user(self, user_id: str, *, spec: Dict[str, Any]) -> None:
        """Create an internal user with a fixed ``user_id``, or update it (2b user).

        Internal-user limits are **flat** (``rpm_limit``/``tpm_limit``), enforced
        aggregated across all of the user's keys (in-memory) — verified
        2026-06-22. ``auto_create_key`` is disabled: we mint the scoped key
        ourselves with a deterministic value so routing never has to read it back.
        """
        resp = await self._client.post(
            f"{self._base_url}/user/new",
            headers=self._headers,
            json={"user_id": user_id, "auto_create_key": False, **spec},
        )
        if resp.status_code == 200:
            return
        upd = await self._client.post(
            f"{self._base_url}/user/update",
            headers=self._headers,
            json={"user_id": user_id, **spec},
        )
        upd.raise_for_status()

    async def upsert_scoped_key(
        self,
        key: str,
        *,
        alias: str,
        user_id: str,
        team_id: str,
        models: list[str],
    ) -> None:
        """Create/refresh a per-(user,project) key bound to its team + owner.

        The key itself carries **no** limits — it inherits the team's per-model
        caps and the internal user's flat caps, which is the whole point: one key
        per (user, project), enforcement on the shared team/user objects. ``key``
        is deterministic (HMAC) so it survives restarts; ``user_id``/``team_id``
        bind it on create and never change for a given (user, project) pair, so
        the update path only needs to refresh the allowed ``models`` list.
        """
        resp = await self._client.post(
            f"{self._base_url}/key/generate",
            headers=self._headers,
            json={
                "key": key,
                "key_alias": alias,
                "user_id": user_id,
                "team_id": team_id,
                "models": models,
            },
        )
        if resp.status_code == 200:
            return
        upd = await self._client.post(
            f"{self._base_url}/key/update",
            headers=self._headers,
            json={"key": key, "models": models},
        )
        upd.raise_for_status()


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


def compute_fleet_key(master_key: str) -> str:
    """Deterministic, secret value for the shared fleet key.

    HMAC of a fixed label under the master key → stable across orchestrator
    restarts (routing recomputes it, no persistence needed) yet unguessable
    without the master key.
    """
    digest = hmac.new(
        master_key.encode(), FLEET_KEY_ALIAS.encode(), hashlib.sha256
    ).hexdigest()
    return f"sk-srw-fleet-{digest[:40]}"


def get_fleet_key() -> str | None:
    """The shared fleet key for agent routing, or None until it's provisioned.

    Returns None (→ routing falls back to the master key) until the sync loop
    has created the key in LiteLLM with its limits, so agents never present a
    key the gateway doesn't know yet.
    """
    if not _fleet_key_ready:
        return None
    master_key = os.getenv("LITELLM_MASTER_KEY", "").strip()
    return compute_fleet_key(master_key) if master_key else None


def _parse_backstop() -> Dict[str, Dict[str, int]]:
    """Parse ``LITELLM_BACKSTOP`` (JSON) → ``{model_id|'*': {'rpm'|'tpm': int}}``.

    The upstream's real capacity, keyed by catalog model_id with a ``'*'``
    default. Empty / malformed → ``{}`` (the fleet key still mints, unlimited, so
    agents stay off the admin master key — the security half of 2a always lands).
    """
    raw = os.getenv("LITELLM_BACKSTOP", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        logger.warning("LITELLM_BACKSTOP is not valid JSON; ignoring backstop")
        return {}


def _build_fleet_limits(
    model_ids: list[str], backstop: Dict[str, Dict[str, int]]
) -> Dict[str, Any]:
    """Expand the per-model / ``'*'``-default capacity config into key limits.

    This is the category→model_names expansion (capability-check gap 1) in
    miniature: ``'*'`` fans out to every registered model so a single number can
    cap the whole fleet, with per-model override.
    """
    default = backstop.get("*", {})
    model_rpm: Dict[str, int] = {}
    model_tpm: Dict[str, int] = {}
    for m in model_ids:
        spec = backstop.get(m, default)
        if spec.get("rpm"):
            model_rpm[m] = int(spec["rpm"])
        if spec.get("tpm"):
            model_tpm[m] = int(spec["tpm"])
    out: Dict[str, Any] = {}
    if model_rpm:
        out["model_rpm_limit"] = model_rpm
    if model_tpm:
        out["model_tpm_limit"] = model_tpm
    return out


async def ensure_fleet_key(
    client: LiteLLMClient, postgres_db: Any, master_key: str
) -> None:
    """Provision the shared, non-admin fleet key carrying the aggregate backstop.

    Idempotent: re-asserts the deterministic key value, scopes it to the
    currently-registered models, and only calls the gateway when the resolved
    limit set changed. Flips ``_fleet_key_ready`` so routing can switch agents
    onto it.
    """
    global _fleet_key_ready, _fleet_spec_hash
    desired = await build_desired_models(postgres_db)
    model_ids = sorted(spec["model_name"] for spec in desired.values())
    if not model_ids:
        return  # nothing registered yet — nothing to scope the key to
    limits = _build_fleet_limits(model_ids, _parse_backstop())
    # Always send both limit dicts (empty = cleared). LiteLLM's /key/update leaves
    # omitted fields untouched, so lowering or removing a backstop only propagates
    # if the field is present — verified that an empty dict clears, not blocks.
    spec = {
        "models": model_ids,
        "model_rpm_limit": limits.get("model_rpm_limit", {}),
        "model_tpm_limit": limits.get("model_tpm_limit", {}),
    }
    spec_hash = json.dumps(spec, sort_keys=True)
    if _fleet_key_ready and spec_hash == _fleet_spec_hash:
        return
    await client.upsert_key(
        compute_fleet_key(master_key), alias=FLEET_KEY_ALIAS, spec=spec
    )
    _fleet_key_ready = True
    _fleet_spec_hash = spec_hash
    logger.info(
        "Fleet backstop key ensured: %d model(s), limits=%s",
        len(model_ids),
        limits or "none (off master key only)",
    )


# ---------------------------------------------------------------------------
# Slice 2b — per-user / per-project rate limits (scoped virtual keys).
#
# Enforcement subjects map onto LiteLLM as: project → **team** (per-model
# ``model_rpm_limit`` dict), user → **internal user** (flat ``rpm_limit``). Both
# enforce in-memory without Redis (verified 2026-06-22). An agent presents one
# **scoped key** per (user, project) that is bound to that project's team and
# that user's internal-user record; the key carries no limits of its own, so the
# team + user objects do the aggregating. The aggregate fleet backstop (2a) stays
# the fallback for jobs missing a user or project. The policy (category→models +
# per-project / per-user limits) rides in the ``LITELLM_RATE_POLICY`` env as JSON,
# the chart's ``litellm.ratePolicy`` value — file-driven for v1; a DB table + admin
# UI can replace the source later without touching this enforcement plumbing.
# ---------------------------------------------------------------------------


def _team_id_for_project(project_id: str) -> str:
    """Deterministic LiteLLM team id for an SRW project."""
    return f"srw-proj-{project_id}"


def _internal_user_id_for(user_id: str) -> str:
    """Deterministic LiteLLM internal-user id for an SRW user."""
    return f"srw-user-{user_id}"


def compute_scoped_key(master_key: str, user_id: str, project_id: str) -> str:
    """Deterministic, secret value for a (user, project) scoped key.

    Same construction as :func:`compute_fleet_key` — HMAC of the (user, project)
    pair under the master key — so routing recomputes it without persistence, yet
    it's unguessable without the master key. Distinct ``sk-srw-`` shape from the
    fleet key's ``sk-srw-fleet-``.
    """
    label = f"srw-scoped:{user_id}:{project_id}"
    digest = hmac.new(master_key.encode(), label.encode(), hashlib.sha256).hexdigest()
    return f"sk-srw-{digest[:48]}"


def _parse_rate_policy() -> Dict[str, Any]:
    """Parse ``LITELLM_RATE_POLICY`` (JSON) → the per-user/project limit policy.

    Schema (all optional)::

        {
          "categories": {"large": ["modelA", "modelB"], "small": ["modelC"]},
          "projects": {"default": {"large": {"rpm": 30}}, "<pid>": {"*": {"rpm": 10}}},
          "users":    {"default": {"rpm": 120}, "<uid>": {"rpm": 300}}
        }

    Project specs are keyed by **category** (or ``'*'`` = every registered model);
    user specs are **flat** rpm/tpm. Empty / malformed → ``{}`` (no per-entity
    limits; agents still ride scoped keys off the master key — the attribution +
    off-admin-key half always lands, mirroring the fleet key's behavior).
    """
    raw = os.getenv("LITELLM_RATE_POLICY", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        logger.warning("LITELLM_RATE_POLICY is not valid JSON; ignoring rate policy")
        return {}


def _category_models(
    policy: Dict[str, Any], category: str, model_ids: list[str]
) -> list[str]:
    """Resolve a project-spec category to registered model_names (validation guard).

    ``'*'`` fans out to every registered model. Otherwise looks the category up in
    the policy's ``categories`` map and keeps only models that are actually
    registered in the gateway — an entry naming an unknown model is **skipped with
    a warning**, never silently applied (capability gap 1: LiteLLM keys limits by
    exact model_name and drops a mismatch without erroring).
    """
    if category == "*":
        return list(model_ids)
    registered = set(model_ids)
    named = (policy.get("categories") or {}).get(category) or []
    valid = [m for m in named if m in registered]
    unknown = [m for m in named if m not in registered]
    if unknown:
        logger.warning(
            "Rate policy: category %r names unregistered model(s) %s — skipped "
            "(would be silently unthrottled by LiteLLM)",
            category,
            sorted(set(unknown)),
        )
    return valid


def _team_limits_for_project(
    policy: Dict[str, Any], project_id: str, model_ids: list[str]
) -> Dict[str, Any]:
    """Build a team's ``model_rpm_limit`` / ``model_tpm_limit`` from the policy.

    Looks up the project's spec (its own id, else ``'default'``), expands each
    category → model_names, and emits per-model dicts. A category that matches no
    registered model is warned about, not dropped silently.
    """
    projects = policy.get("projects") or {}
    spec = projects.get(project_id) or projects.get("default") or {}
    model_rpm: Dict[str, int] = {}
    model_tpm: Dict[str, int] = {}
    for category, limits in spec.items():
        if not isinstance(limits, dict):
            continue
        models = _category_models(policy, category, model_ids)
        if not models:
            logger.warning(
                "Rate policy: project %s category %r matched no registered models",
                project_id,
                category,
            )
            continue
        for m in models:
            if limits.get("rpm"):
                model_rpm[m] = int(limits["rpm"])
            if limits.get("tpm"):
                model_tpm[m] = int(limits["tpm"])
    out: Dict[str, Any] = {}
    if model_rpm:
        out["model_rpm_limit"] = model_rpm
    if model_tpm:
        out["model_tpm_limit"] = model_tpm
    return out


def _user_limits(policy: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    """Build an internal user's flat ``rpm_limit`` / ``tpm_limit`` from the policy."""
    users = policy.get("users") or {}
    spec = users.get(user_id) or users.get("default") or {}
    out: Dict[str, Any] = {}
    if spec.get("rpm"):
        out["rpm_limit"] = int(spec["rpm"])
    if spec.get("tpm"):
        out["tpm_limit"] = int(spec["tpm"])
    return out


async def ensure_scoped_key(
    client: LiteLLMClient,
    postgres_db: Any,
    master_key: str,
    *,
    user_id: str | None,
    project_id: str | None,
) -> str | None:
    """Provision the (user, project) scoped key + its team/internal-user, return it.

    Returns ``None`` when the job has no user or no project — the caller then
    falls back to the shared fleet key (2a), which still caps the aggregate. Else
    idempotently upserts the project's team (per-model limits), the user's
    internal-user record (flat limits), and the deterministic scoped key bound to
    both, then returns the key value.

    Hash-gated by ``_scoped_ensured`` so a repeat dispatch for the same
    (user, project) with unchanged policy/models issues **no** gateway calls —
    only the resolve + hash. The team/user limit dicts are **always sent in full**
    (empty = cleared) so lowering or removing a limit propagates, same lesson as
    the fleet key's ``/key/update``.
    """
    if not user_id or not project_id:
        return None
    desired = await build_desired_models(postgres_db)
    model_ids = sorted(spec["model_name"] for spec in desired.values())
    if not model_ids:
        return None

    policy = _parse_rate_policy()
    team_limits = _team_limits_for_project(policy, project_id, model_ids)
    user_limits = _user_limits(policy, user_id)
    team_id = _team_id_for_project(project_id)
    internal_uid = _internal_user_id_for(user_id)
    key = compute_scoped_key(master_key, user_id, project_id)

    spec_hash = json.dumps(
        {
            "models": model_ids,
            "team": team_limits,
            "user": user_limits,
            "team_id": team_id,
            "uid": internal_uid,
        },
        sort_keys=True,
    )
    if _scoped_ensured.get((user_id, project_id)) == spec_hash:
        return key

    # Team (project): always send both limit dicts so removals propagate.
    await client.upsert_team(
        team_id,
        alias=f"srw-proj-{project_id}",
        spec={
            "models": model_ids,
            "model_rpm_limit": team_limits.get("model_rpm_limit", {}),
            "model_tpm_limit": team_limits.get("model_tpm_limit", {}),
        },
    )
    # Internal user: flat limits. Send explicit null to clear when absent so a
    # removed per-user override propagates (same omitted-field caveat as keys).
    await client.upsert_internal_user(
        internal_uid,
        spec={
            "rpm_limit": user_limits.get("rpm_limit"),
            "tpm_limit": user_limits.get("tpm_limit"),
        },
    )
    await client.upsert_scoped_key(
        key,
        alias=f"srw-scoped-{user_id}-{project_id}",
        user_id=internal_uid,
        team_id=team_id,
        models=model_ids,
    )
    _scoped_ensured[(user_id, project_id)] = spec_hash
    logger.info(
        "Scoped key ensured: user=%s project=%s (%d models, team_limits=%s, "
        "user_limits=%s)",
        user_id,
        project_id,
        len(model_ids),
        team_limits or "none",
        user_limits or "none",
    )
    return key


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
                    await ensure_fleet_key(client, postgres_db, master_key)
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
