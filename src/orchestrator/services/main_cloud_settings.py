"""System Settings — Main Cloud (Phase 4, Admin-only).

Extracted verbatim from ``orchestrator.main`` (R1.B04 lane M).

These operations drive the cockpit admin "Cloud Storage" panel. GETs
return the current immutable instance snapshot with secrets stripped, PUT
remotely attests and CAS-activates a new instance, and POST /test does a
dry-run connection check without persisting.

Secret handling: non-secret fields (URLs, usernames, quota) are stored
in the `value` JSONB column. Secret fields (passwords, client secrets)
are referenced via `credentials_ref` — a pointer like
`env:OPENCLOUD_KEYCLOAK_CLIENT_SECRET` that the loader resolves against
the orchestrator's own environment. This keeps secrets in Vault/ESO/.env
and lets the UI manage only the non-secret knobs.

Two properties are load-bearing and moved unchanged:

* **What leaves the process.** ``_sanitize_main_cloud_value`` drops every
  secret-field key from an incoming body before it can reach JSONB, and
  ``_env_var_provenance`` answers "is it set?" and nothing more. No read or
  write path in this module returns, logs, or echoes a secret value.
* **Installation authority.** ``cloud_router.active`` raising when no active
  backend instance is bound is correct and is never softened here; the
  backfill refuses to guess an installation, and both it and the reload path
  re-attest against the live installation before trusting a proof.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import HTTPException

from orchestrator.services.cloud import build_backend
from orchestrator.services.cloud.instance_registry import (
    activate_main_cloud_config,
    reload_active_main_cloud_instance,
)
from orchestrator.services.cloud.reload import fire_reload
from orchestrator.services import thread_mount_rows

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MainCloudSettingsDependencies:
    """Collaborators for one main-cloud settings operation, per invocation.

    ``store`` is main's ``postgres_db`` and ``cloud_router`` its
    ``main_cloud_router``. ``rebind_cloud_router`` is the application-owned
    setter from port §3.3: the router object is normally mutated in place by
    ``replace_active``, and nothing in the moved code reassigns it — but a
    service must never be the thing that rebinds an application global, so the
    seam is declared here rather than invented later.

    ``thread_mount_dependencies`` is main's ``_thread_mount_dependencies``
    factory: the transport repair rebuilds mount rows through the same
    builder thread create uses, so it needs that lane's collaborators, and it
    needs them resolved per call for the same rebinding reason as the two
    above.
    """

    store: Any
    cloud_router: Any
    rebind_cloud_router: Callable[[Any], None]
    thread_mount_dependencies: Callable[[], Any]


_MAIN_CLOUD_NONSECRET_FIELDS_BY_BACKEND: dict[str, list[str]] = {
    "nextcloud": [
        "base_url",
        "public_url",
        "admin_user",
        "agent_user",
    ],
    "opencloud": [
        "base_url",
        "public_url",
        "keycloak_issuer",
        "keycloak_client_id",
        "admin_role_claim_value",
        "default_quota_bytes",
    ],
}

_MAIN_CLOUD_SECRET_FIELDS_BY_BACKEND: dict[str, list[str]] = {
    "nextcloud": ["admin_password", "agent_password", "oidc_client_secret"],
    "opencloud": ["keycloak_client_secret"],
}

_MAIN_CLOUD_ALLOWED_BACKENDS = {"nextcloud", "opencloud"}


def _sanitize_main_cloud_value(
    backend_id: str, raw_value: dict[str, Any]
) -> dict[str, Any]:
    """Strip unknown keys + secret fields from an incoming overlay body.

    Secrets are never stored in JSONB — they always come from env vars
    resolved via `credentials_ref`. The sanitizer drops any secret-field
    key from the incoming dict so a careless UI PUT cannot accidentally
    persist a client secret into `system_settings.value`.
    """
    nonsecret = _MAIN_CLOUD_NONSECRET_FIELDS_BY_BACKEND.get(backend_id, [])
    secret = set(_MAIN_CLOUD_SECRET_FIELDS_BY_BACKEND.get(backend_id, []))
    clean: dict[str, Any] = {"backend_id": backend_id}
    for key in nonsecret:
        if key in raw_value and raw_value[key] not in (None, ""):
            clean[key] = raw_value[key]
    # Record which fields are secret-credential-sourced so the loader
    # knows to resolve them via credentials_ref at read time.
    if secret:
        clean["__secret_fields__"] = sorted(secret)
    return clean


def _current_effective_config(
    *, dependencies: MainCloudSettingsDependencies
) -> dict[str, Any]:
    """Read the active backend's current config shape for GET responses."""
    active = dependencies.cloud_router.active
    backend_id = active.backend_id
    result: dict[str, Any] = {
        "backend_id": backend_id,
        "backend_instance_id": active.backend_instance_id,
        "is_initialized": active.is_initialized,
        "is_configured": active.is_configured,
    }

    # Non-secret fields come directly from the backend's settings where
    # possible. NextcloudBackend reads env vars into private attrs;
    # OpenCloudBackend holds a full settings dataclass.
    if backend_id == "nextcloud":
        # Attributes follow the adapter's internal names.
        result.update(
            {
                "base_url": getattr(active, "_base_url", None),
                "public_url": getattr(active, "_public_url", None),
                "admin_user": getattr(active, "_admin_user", None),
                "agent_user": getattr(active, "_agent_user", None),
            }
        )
    elif backend_id == "opencloud":
        settings = getattr(active, "_settings", None)
        if settings is not None:
            result.update(
                {
                    "base_url": str(settings.base_url),
                    "public_url": str(settings.public_url),
                    "keycloak_issuer": str(settings.keycloak_issuer),
                    "keycloak_client_id": settings.keycloak_client_id,
                    "admin_role_claim_value": settings.admin_role_claim_value,
                    "default_quota_bytes": settings.default_quota_bytes,
                }
            )
    return result


def _env_var_provenance(env_name: str) -> dict[str, Any]:
    """Report whether a secret env var is set, without leaking its value."""
    val = os.getenv(env_name)
    return {
        "env_var": env_name,
        "set": bool(val),
    }


async def get_main_cloud_settings(
    *, dependencies: MainCloudSettingsDependencies
) -> dict[str, Any]:
    """Return the current effective main-cloud config + persisted overlay.

    Admin-only. The response is safe to log: every secret field is
    replaced with its env-var provenance (name + set/unset flag + length).
    """
    effective = _current_effective_config(dependencies=dependencies)

    try:
        active_row = await dependencies.store.get_active_main_cloud_backend_instance()
    except Exception:
        active_row = None
    authority = active_row.get("authority") if isinstance(active_row, dict) else None
    overlay_value: dict[str, Any] = {}
    overlay_updated_at: Optional[str] = None
    credentials_ref: Optional[str] = None
    activation_revision = 0
    secret_refs: dict[str, str] = {}
    if authority is not None:
        overlay_value = authority.routing
        secret_refs = authority.secret_refs
        overlay_value["__secret_fields__"] = sorted(secret_refs)
        distinct_refs = set(secret_refs.values())
        if len(distinct_refs) == 1:
            credentials_ref = next(iter(distinct_refs))
        activated_at = active_row.get("activated_at")
        overlay_updated_at = (
            activated_at.isoformat() if activated_at is not None else None
        )
        activation_revision = int(active_row.get("activation_revision") or 0)
        effective.update(authority.routing)
        effective["backend_instance_id"] = authority.backend_instance_id

    secret_provenance: dict[str, dict[str, Any]] = {}
    for field, reference in secret_refs.items():
        secret_provenance[field] = _env_var_provenance(reference.removeprefix("env:"))

    return {
        "effective": effective,
        "activation_revision": activation_revision,
        "backend_instance": (
            {
                "id": authority.backend_instance_id,
                "routing_sha256": authority.routing_sha256,
                "installation_proof_sha256": (authority.installation_proof_sha256),
                "secret_revision": authority.secret_revision,
            }
            if authority is not None
            else None
        ),
        "overlay": {
            # Compatibility name for the existing Cockpit form. This is the
            # immutable active routing snapshot, not system_settings authority.
            "present": authority is not None,
            "value": overlay_value,
            "credentials_ref": credentials_ref,
            "updated_at": overlay_updated_at,
            "updated_by": None,
        },
        "secrets": secret_provenance,
        "allowed_backends": sorted(_MAIN_CLOUD_ALLOWED_BACKENDS),
    }


async def put_main_cloud_settings(
    *,
    body: dict[str, Any],
    admin: dict[str, Any],
    dependencies: MainCloudSettingsDependencies,
) -> dict[str, Any]:
    """Attest and CAS-activate a new main-cloud backend instance.

    Admin-only. The request body is ``{"value": {...}, "credentials_ref": "env:..."}``:

    * ``value.backend_id`` must be one of ``allowed_backends``.
    * Secret fields in ``value`` are silently dropped by the sanitizer —
      never persist secrets in the DB. Rotate via the secret store.
    * ``credentials_ref`` is an optional pointer (e.g. ``env:NEW_VAR``)
      that the loader resolves for secret fields at read time.
    * ``expected_activation_revision`` must match the GET snapshot.
    * Routing edits create a new immutable instance UUID. Secret-reference
      edits rotate only the exact same proven installation.
    * Other replicas resolve the durable pointer via the pg_notify LISTEN task.
    """
    postgres_db = dependencies.store

    value_in = body.get("value") or {}
    if not isinstance(value_in, dict):
        raise HTTPException(status_code=400, detail="`value` must be an object")
    backend_id = value_in.get("backend_id")
    if backend_id not in _MAIN_CLOUD_ALLOWED_BACKENDS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown backend_id {backend_id!r}; "
                f"must be one of {sorted(_MAIN_CLOUD_ALLOWED_BACKENDS)}"
            ),
        )
    credentials_ref = body.get("credentials_ref")
    if credentials_ref is not None and not isinstance(credentials_ref, str):
        raise HTTPException(
            status_code=400, detail="`credentials_ref` must be a string or null"
        )
    expected_activation_revision = body.get("expected_activation_revision")
    if (
        type(expected_activation_revision) is not int
        or expected_activation_revision < 0
    ):
        raise HTTPException(
            status_code=400,
            detail="`expected_activation_revision` must be a non-negative integer",
        )

    clean_value = _sanitize_main_cloud_value(backend_id, value_in)

    # Validate the proposed config before persisting — raises on
    # missing required fields so we fail fast with a 422-style message.
    from orchestrator.services.cloud.config import (
        load_main_cloud_config,
        missing_secret_envs,
    )

    probe_overlay = {
        "value": clean_value,
        "credentials_ref": credentials_ref,
    }
    try:
        load_main_cloud_config(db_overlay=probe_overlay)
    except Exception as e:
        raise HTTPException(
            status_code=400, detail=f"invalid main cloud config: {e}"
        ) from e

    # Fail loud if the backend's real secrets are not wired (Issue 5). The
    # loader above validates the *shape* but silently substitutes built-in dev
    # defaults for missing secrets, so a config that could only ever connect
    # with `admin` / `agent-service-dev` would otherwise persist + activate and
    # fail much later at the first cloud call (surviving restarts). Refuse here,
    # naming the exact env var(s) to set.
    missing = missing_secret_envs(backend_id, probe_overlay)
    if missing:
        names = ", ".join(sorted({m["env_var"] for m in missing}))
        raise HTTPException(
            status_code=400,
            detail=(
                f"secret env not set for backend {backend_id!r}: {names}. "
                "Wire the secret(s) into the orchestrator env (Helm/Vault) or "
                "point `credentials_ref` at a set env var, then retry. Refusing "
                "to activate a backend that would fall back to built-in dev "
                "credentials."
            ),
        )

    actor = str(admin.get("id") or admin.get("email") or "admin")
    try:
        activated = await activate_main_cloud_config(
            postgres_db,
            dependencies.cloud_router,
            db_overlay=probe_overlay,
            expected_activation_revision=expected_activation_revision,
            activated_by=actor,
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=(
                "main cloud backend attestation failed; no unverified adapter "
                f"was activated: {e}"
            ),
        ) from e
    if activated is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "main cloud activation authority changed; reload the current "
                "settings and retry"
            ),
        )

    # Fan-out to other replicas via pg_notify. Best-effort.
    authority = activated["authority"]
    try:
        await postgres_db.delete_system_setting("main_cloud")
    except Exception:
        logger.warning(
            "Failed to remove inert legacy main_cloud setting after activation",
            exc_info=True,
        )
    await fire_reload(postgres_db, authority.backend_instance_id)
    return {
        "status": "ok",
        "backend_id": backend_id,
        "backend_instance_id": authority.backend_instance_id,
        "activation_revision": activated["activation_revision"],
        "reloaded": True,
    }


async def test_main_cloud_settings(
    *,
    body: dict[str, Any],
    dependencies: MainCloudSettingsDependencies,
) -> dict[str, Any]:
    """Dry-run a proposed main-cloud config without persisting.

    Builds a backend from the proposed overlay, calls
    ``ensure_initialized()``, and tears it down. Returns whether the
    probe succeeded plus a short detail string. Useful for "Test"
    buttons in the admin UI before the operator commits to saving.
    """
    value_in = body.get("value") or {}
    backend_id = value_in.get("backend_id")
    if backend_id not in _MAIN_CLOUD_ALLOWED_BACKENDS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown backend_id {backend_id!r}",
        )
    credentials_ref = body.get("credentials_ref")

    clean_value = _sanitize_main_cloud_value(backend_id, value_in)
    probe_overlay = {
        "value": clean_value,
        "credentials_ref": credentials_ref,
    }

    # Issue 5: surface unwired secrets as the precise reason, instead of letting
    # the probe connect with built-in dev credentials and report a cryptic
    # upstream-auth failure.
    from orchestrator.services.cloud.config import missing_secret_envs

    missing = missing_secret_envs(backend_id, probe_overlay)
    if missing:
        names = ", ".join(sorted({m["env_var"] for m in missing}))
        return {
            "ok": False,
            "detail": (
                f"secret env not set for backend {backend_id!r}: {names} "
                "(would fall back to built-in dev credentials). Wire the "
                "secret(s) or set `credentials_ref` before testing."
            ),
        }

    try:
        probe_backend = build_backend(db_overlay=probe_overlay)
    except Exception as e:
        return {"ok": False, "detail": f"build_backend failed: {e}"}

    try:
        try:
            ok = await probe_backend.ensure_initialized()
        except Exception as e:
            return {"ok": False, "detail": f"ensure_initialized raised: {e}"}
        if not ok:
            return {
                "ok": False,
                "detail": "backend reported not initialized — check config + upstream",
            }
        health = await probe_backend.health_check()
        return {
            "ok": health.ok,
            "detail": health.detail or "",
            "latency_ms": health.latency_ms,
        }
    finally:
        try:
            await probe_backend.close()
        except Exception:
            pass


async def reload_main_cloud_settings(
    *, dependencies: MainCloudSettingsDependencies
) -> dict[str, Any]:
    """Force a local re-attestation of the durable active instance.

    Admin-only. Useful when an operator has rotated a secret out-of-band
    (new Keycloak client secret in .env) and wants this orchestrator
    replica to rebuild its client after an out-of-band secret-value update.
    The immutable secret reference and installation proof remain unchanged.
    """
    try:
        ok = await reload_active_main_cloud_instance(
            dependencies.store,
            dependencies.cloud_router,
            force_rebuild=True,
        )
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"active instance re-attestation failed: {e}"
        ) from e
    if ok is not True:
        raise HTTPException(
            status_code=409,
            detail="active instance changed during re-attestation; retry",
        )
    return {
        "status": "ok",
        "backend_id": dependencies.cloud_router.active.backend_id,
        "backend_instance_id": dependencies.cloud_router.active_instance_id,
    }


async def backfill_main_cloud_instance_authority(
    *, apply: bool, dependencies: MainCloudSettingsDependencies
) -> dict[str, Any]:
    """Stamp pre-0186 rows with the installation they actually live on.

    Admin-only. **Dry run by default** — pass ``?apply=true`` to write.

    0186 added ``main_cloud_backend_instance_id`` nullable with no backfill, so
    rows stamped before it name a provider but no installation. The router
    fails closed on that shape, which is correct for effects: those projects
    cannot grant membership, share, or have their folder deleted, because we
    cannot say *which* installation to act on.

    This is deliberately NOT a SQL migration. The design
    (knowledge-base/knowledge/features/protected_session_lifecycle_and_mount_readiness.md)
    requires "an operator-attested single-installation mapping plus a verified
    remote proof", and a migration running inside psql at startup can obtain
    neither. Stamping without that proof would launder a guess into recorded
    authority — silently, permanently, and unquestioned by everything
    downstream. A loud refusal is recoverable; a wrong instance UUID is not.

    So the safety argument here is:

    1. the active instance is **re-attested against the live installation**
       before anything is read, so its proof reflects reality now;
    2. every provider named by an unstamped row must resolve to exactly one
       *installation* in the registry. Two registry rows for one provider are
       not automatically ambiguous — they are commonly two routing snapshots
       of the same installation, which share an
       ``installation_proof_sha256``. Two distinct **proofs** are the genuine
       ambiguity, and abort;
    3. a provider we cannot re-attest right now (not the active backend) is
       never stamped, because nothing proves where its rows live.
    """
    postgres_db = dependencies.store

    survey = await postgres_db.survey_unstamped_main_cloud_rows()
    unstamped_projects = survey["projects"]
    unstamped_threads = survey["threads"]
    if not unstamped_projects and not unstamped_threads:
        return {
            "status": "noop",
            "applied": False,
            "detail": "No rows carry a provider without its backend instance.",
            "projects": 0,
            "threads": 0,
        }

    providers = sorted(
        {str(r["main_cloud_backend"]) for r in unstamped_projects}
        | {str(r["main_cloud_backend"]) for r in unstamped_threads}
    )

    # (1) Re-attest the active installation before trusting its proof.
    try:
        reattested = await reload_active_main_cloud_instance(
            postgres_db,
            dependencies.cloud_router,
            force_rebuild=True,
        )
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"cannot verify the live installation proof: {e}",
        ) from e
    if reattested is not True:
        raise HTTPException(
            status_code=409,
            detail="active instance changed during re-attestation; retry",
        )

    active = await postgres_db.get_active_main_cloud_backend_instance()
    if not active:
        raise HTTPException(
            status_code=409,
            detail="no active main-cloud instance to attest against",
        )
    active_authority = active["authority"]

    registry = await postgres_db.list_main_cloud_backend_instances()

    # (2)+(3) Resolve one installation per provider, or refuse.
    plan: list[dict[str, Any]] = []
    for provider in providers:
        proofs = {
            a.installation_proof_sha256 for a in registry if a.backend_id == provider
        }
        if len(proofs) != 1:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"provider {provider!r} has {len(proofs)} distinct "
                    "installations in the registry; historical rows cannot be "
                    "attributed to one of them automatically. Resolve this "
                    "mapping by hand."
                ),
            )
        if provider != active_authority.backend_id:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"provider {provider!r} is not the active backend "
                    f"({active_authority.backend_id!r}), so its installation "
                    "cannot be re-attested right now. Activate it first."
                ),
            )
        if proofs != {active_authority.installation_proof_sha256}:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"provider {provider!r} registry proof does not match the "
                    "installation just attested; refusing to attribute "
                    "historical rows to it."
                ),
            )
        plan.append(
            {
                "backend_id": provider,
                "backend_instance_id": active_authority.backend_instance_id,
                "installation_proof_sha256": (
                    active_authority.installation_proof_sha256
                ),
                "projects": [
                    {
                        "id": str(r["id"]),
                        "name": r.get("name"),
                        "status": r.get("status"),
                    }
                    for r in unstamped_projects
                    if str(r["main_cloud_backend"]) == provider
                ],
                "threads": [
                    str(r["id"])
                    for r in unstamped_threads
                    if str(r["main_cloud_backend"]) == provider
                ],
            }
        )

    if not apply:
        return {
            "status": "dry_run",
            "applied": False,
            "detail": "Re-run with ?apply=true to write these stamps.",
            "plan": plan,
            "projects": len(unstamped_projects),
            "threads": len(unstamped_threads),
        }

    stamped = {"projects": 0, "threads": 0}
    for entry in plan:
        counts = await postgres_db.stamp_main_cloud_instance_authority(
            backend_id=entry["backend_id"],
            backend_instance_id=entry["backend_instance_id"],
        )
        stamped["projects"] += counts["projects"]
        stamped["threads"] += counts["threads"]
        logger.warning(
            "main-cloud instance authority backfill: stamped %d project(s) and "
            "%d thread(s) for provider %r with instance %s (proof %s)",
            counts["projects"],
            counts["threads"],
            entry["backend_id"],
            entry["backend_instance_id"],
            entry["installation_proof_sha256"],
        )

    return {
        "status": "ok",
        "applied": True,
        "plan": plan,
        "projects": stamped["projects"],
        "threads": stamped["threads"],
    }


async def repair_thread_mount_transport(
    *, apply: bool, dependencies: MainCloudSettingsDependencies
) -> dict[str, Any]:
    """Re-derive the transport of partial project mount rows.

    Admin-only. **Dry run by default** — pass ``?apply=true`` to write.

    ``thread_mounts`` rows minted while their project was still unstamped
    (pre-0186) carry a provider name but no installation and no WebDAV URL,
    and the instance-authority backfill leaves them exactly so: mount rows
    are only re-derived for a thread that has none. At delivery one such row
    is fatal to the whole set — ``_build_agent_cloud_mount`` mounts every row
    or falls back to the legacy session folder — so a stamped project still
    yields a session that writes to ``sessions/<id>``.

    Every row is rebuilt through the builder that creates rows
    (``thread_mount_rows.build_project_mount_row``) against the project's
    *stamped* installation, and written only when every transport column
    resolved. Nothing here guesses: an unstamped project is skipped with a
    pointer at the backfill, an installation this replica cannot resolve is
    skipped, and a rebuilt row that is itself partial is never written — the
    repair must not mint the shape it exists to remove. Rows are updated in
    place, so mount ids and the collision-suffixed ``target_path`` decided at
    create time survive. Re-running is a no-op.
    """
    postgres_db = dependencies.store

    partial = await postgres_db.survey_partial_thread_mounts()
    if not partial:
        return {
            "status": "noop",
            "applied": False,
            "detail": "No project mount row lacks its transport.",
            "rows": 0,
            "repairable": 0,
            "repaired": 0,
            "skipped": 0,
        }

    mount_dependencies = dependencies.thread_mount_dependencies()
    plan: list[dict[str, Any]] = []
    writes: dict[str, dict[str, Any]] = {}
    projects: dict[str, Any] = {}
    for row in partial:
        mount_id = str(row["id"])
        entry: dict[str, Any] = {
            "mount_id": mount_id,
            "thread_id": str(row["thread_id"]),
            "thread_status": row.get("thread_status"),
            "mount_kind": row.get("mount_kind"),
            "target_path": row.get("target_path"),
            "project_id": str(row["source_ref"]) if row.get("source_ref") else None,
        }
        project_id = entry["project_id"]
        if not project_id:
            plan.append({**entry, "action": "skip", "reason": "no_project"})
            continue
        if project_id not in projects:
            projects[project_id] = await postgres_db.get_project(project_id)
        project = projects[project_id]
        if not project:
            plan.append({**entry, "action": "skip", "reason": "project_missing"})
            continue
        if project.get("main_cloud_backend") and not project.get(
            "main_cloud_backend_instance_id"
        ):
            plan.append(
                {
                    **entry,
                    "action": "skip",
                    "reason": "project_unstamped",
                    "detail": "run backfill-instance-authority first",
                }
            )
            continue
        try:
            rebuilt = await thread_mount_rows.build_project_mount_row(
                project_id, project, dependencies=mount_dependencies
            )
        except Exception as e:
            logger.warning(
                "thread mount transport repair: rebuilding row %s for project "
                "%s failed: %s",
                mount_id,
                project_id,
                e,
            )
            rebuilt = None
        if rebuilt is None:
            plan.append({**entry, "action": "skip", "reason": "transport_unresolvable"})
            continue
        if rebuilt.get("mount_kind") != row.get("mount_kind"):
            plan.append(
                {
                    **entry,
                    "action": "skip",
                    "reason": "mount_kind_changed",
                    "detail": f"project now yields {rebuilt.get('mount_kind')!r}",
                }
            )
            continue
        writes[mount_id] = {
            "backend_id": str(rebuilt["backend_id"]),
            "backend_instance_id": str(rebuilt["backend_instance_id"]),
            "cloud_handle": rebuilt.get("cloud_handle"),
            "webdav_url": str(rebuilt["webdav_url"]),
            "target_user_sub": rebuilt.get("target_user_sub"),
        }
        plan.append(
            {
                **entry,
                "action": "repair",
                "backend_id": writes[mount_id]["backend_id"],
                "backend_instance_id": writes[mount_id]["backend_instance_id"],
                "webdav_url": writes[mount_id]["webdav_url"],
                "target_user_sub": bool(writes[mount_id]["target_user_sub"]),
            }
        )

    skipped = len(plan) - len(writes)
    if not apply:
        return {
            "status": "dry_run",
            "applied": False,
            "detail": "Re-run with ?apply=true to write these transports.",
            "plan": plan,
            "rows": len(plan),
            "repairable": len(writes),
            "repaired": 0,
            "skipped": skipped,
        }

    repaired = 0
    for entry in plan:
        write = writes.get(entry["mount_id"])
        if write is None:
            continue
        written = await postgres_db.repair_thread_mount_transport(
            entry["mount_id"], **write
        )
        entry["written"] = bool(written)
        repaired += int(bool(written))
    logger.warning(
        "thread mount transport repair: rewrote %d of %d partial row(s); %d skipped",
        repaired,
        len(plan),
        skipped,
    )
    return {
        "status": "ok",
        "applied": True,
        "plan": plan,
        "rows": len(plan),
        "repairable": len(writes),
        "repaired": repaired,
        "skipped": skipped,
    }


async def delete_main_cloud_settings(
    *,
    admin: dict[str, Any],
    dependencies: MainCloudSettingsDependencies,
) -> dict[str, Any]:
    """Attest and activate the current env-described installation.

    History is retained; reset never deletes an instance referenced by an
    existing project, session, grant, or staged review.
    """
    postgres_db = dependencies.store
    current = await postgres_db.get_active_main_cloud_backend_instance()
    expected_revision = (
        int(current.get("activation_revision") or 0) if isinstance(current, dict) else 0
    )
    try:
        activated = await activate_main_cloud_config(
            postgres_db,
            dependencies.cloud_router,
            db_overlay=None,
            expected_activation_revision=expected_revision,
            activated_by=str(admin.get("id") or admin.get("email") or "admin"),
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"env-described main cloud attestation failed: {e}",
        ) from e
    if activated is None:
        raise HTTPException(
            status_code=409,
            detail="main cloud activation authority changed; reload and retry",
        )
    try:
        await postgres_db.delete_system_setting("main_cloud")
    except Exception:
        logger.warning(
            "Failed to remove inert legacy main_cloud setting", exc_info=True
        )
    authority = activated["authority"]
    await fire_reload(postgres_db, authority.backend_instance_id)
    return {
        "status": "ok",
        "existed": current is not None,
        "backend_id": authority.backend_id,
        "backend_instance_id": authority.backend_instance_id,
        "activation_revision": activated["activation_revision"],
    }
