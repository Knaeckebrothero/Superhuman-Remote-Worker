"""HTTP adapters for the admin "Cloud Storage" panel.

Six routes on ``/api/admin/system-settings/main_cloud``. Every one of them
awaits the admin gate first — before the body is inspected, before any
validation refusal — so a non-admin can never learn which backend ids the
deployment accepts or whether a secret env var is wired.

Only two routes need the admin's identity beyond the gate (PUT and DELETE
record an actor on the activation), so those two pass it down; the rest
discard it exactly as ``main`` did.

Route order is part of the contract: the literal ``/test``, ``/reload`` and
``/backfill-instance-authority`` sub-paths are POSTs on a base path whose only
other verbs are GET/PUT/DELETE, so nothing here shadows anything.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Request

from orchestrator.services import main_cloud_settings

router = APIRouter()


@dataclass(frozen=True)
class MainCloudSettingsRouteDependencies:
    """Per-app collaborators plus main's composed ``_require_admin`` gate.

    ``require_admin`` has no import default on purpose: it is a main-local
    composition of ``security.access.require_admin`` with the store, the
    approved-user resolver and the security-event audit already bound.
    """

    operations: main_cloud_settings.MainCloudSettingsDependencies
    require_admin: Callable[[Request], Awaitable[dict[str, Any]]]


def get_main_cloud_settings_dependencies(
    request: Request,
) -> MainCloudSettingsRouteDependencies:
    """Resolve collaborators only from the application handling this request."""
    return request.app.state.main_cloud_settings_dependencies_factory()


# =============================================================================
# System Settings — Main Cloud (Phase 4, Admin-only)
# =============================================================================
# These endpoints drive the cockpit admin "Cloud Storage" panel. GETs
# return the current immutable instance snapshot with secrets stripped, PUT
# remotely attests and CAS-activates a new instance, and POST /test does a
# dry-run connection check without persisting.
#
# Secret handling: non-secret fields (URLs, usernames, quota) are stored
# in the `value` JSONB column. Secret fields (passwords, client secrets)
# are referenced via `credentials_ref` — a pointer like
# `env:OPENCLOUD_KEYCLOAK_CLIENT_SECRET` that the loader resolves against
# the orchestrator's own environment. This keeps secrets in Vault/ESO/.env
# and lets the UI manage only the non-secret knobs.


@router.get("/api/admin/system-settings/main_cloud")
async def get_main_cloud_settings(
    request: Request,
    *,
    dependencies: MainCloudSettingsRouteDependencies = Depends(
        get_main_cloud_settings_dependencies
    ),
) -> dict[str, Any]:
    """Return the current effective main-cloud config + persisted overlay.

    Admin-only. The response is safe to log: every secret field is
    replaced with its env-var provenance (name + set/unset flag + length).
    """
    await dependencies.require_admin(request)
    return await main_cloud_settings.get_main_cloud_settings(
        dependencies=dependencies.operations
    )


@router.put("/api/admin/system-settings/main_cloud")
async def put_main_cloud_settings(
    body: dict[str, Any],
    request: Request,
    *,
    dependencies: MainCloudSettingsRouteDependencies = Depends(
        get_main_cloud_settings_dependencies
    ),
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
    admin = await dependencies.require_admin(request)
    return await main_cloud_settings.put_main_cloud_settings(
        body=body, admin=admin, dependencies=dependencies.operations
    )


@router.post("/api/admin/system-settings/main_cloud/test")
async def test_main_cloud_settings(
    body: dict[str, Any],
    request: Request,
    *,
    dependencies: MainCloudSettingsRouteDependencies = Depends(
        get_main_cloud_settings_dependencies
    ),
) -> dict[str, Any]:
    """Dry-run a proposed main-cloud config without persisting.

    Builds a backend from the proposed overlay, calls
    ``ensure_initialized()``, and tears it down. Returns whether the
    probe succeeded plus a short detail string. Useful for "Test"
    buttons in the admin UI before the operator commits to saving.
    """
    await dependencies.require_admin(request)
    return await main_cloud_settings.test_main_cloud_settings(
        body=body, dependencies=dependencies.operations
    )


@router.post("/api/admin/system-settings/main_cloud/reload")
async def reload_main_cloud_settings(
    request: Request,
    *,
    dependencies: MainCloudSettingsRouteDependencies = Depends(
        get_main_cloud_settings_dependencies
    ),
) -> dict[str, Any]:
    """Force a local re-attestation of the durable active instance.

    Admin-only. Useful when an operator has rotated a secret out-of-band
    (new Keycloak client secret in .env) and wants this orchestrator
    replica to rebuild its client after an out-of-band secret-value update.
    The immutable secret reference and installation proof remain unchanged.
    """
    await dependencies.require_admin(request)
    return await main_cloud_settings.reload_main_cloud_settings(
        dependencies=dependencies.operations
    )


@router.post("/api/admin/system-settings/main_cloud/backfill-instance-authority")
async def backfill_main_cloud_instance_authority(
    request: Request,
    apply: bool = False,
    *,
    dependencies: MainCloudSettingsRouteDependencies = Depends(
        get_main_cloud_settings_dependencies
    ),
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
    await dependencies.require_admin(request)
    return await main_cloud_settings.backfill_main_cloud_instance_authority(
        apply=apply, dependencies=dependencies.operations
    )


@router.delete("/api/admin/system-settings/main_cloud")
async def delete_main_cloud_settings(
    request: Request,
    *,
    dependencies: MainCloudSettingsRouteDependencies = Depends(
        get_main_cloud_settings_dependencies
    ),
) -> dict[str, Any]:
    """Attest and activate the current env-described installation.

    History is retained; reset never deletes an instance referenced by an
    existing project, session, grant, or staged review.
    """
    admin = await dependencies.require_admin(request)
    return await main_cloud_settings.delete_main_cloud_settings(
        admin=admin, dependencies=dependencies.operations
    )
