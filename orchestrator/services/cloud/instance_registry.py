"""Durable activation service for main-cloud backend installations.

The active provider name is not routing authority.  This module is the only
application seam that turns deploy/admin configuration into an attested,
immutable backend-instance row and swaps the process-local router.  Historical
adapters are always rebuilt from their retained instance authority.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from . import MainCloudBackend, MainCloudRouter, build_backend_from_config
from .backend_instance_authority import MainCloudBackendInstanceAuthority
from .config import (
    load_main_cloud_config,
    main_cloud_routing_snapshot,
    main_cloud_secret_references,
)

logger = logging.getLogger(__name__)


def _active_coordinates(value: Any) -> tuple[str, int, str] | None:
    if not isinstance(value, dict):
        return None
    authority = value.get("authority")
    revision = value.get("activation_revision")
    if (
        not isinstance(authority, MainCloudBackendInstanceAuthority)
        or type(revision) is not int
        or revision <= 0
    ):
        return None
    return (
        authority.backend_instance_id,
        revision,
        authority.canonical_json,
    )


async def build_attested_main_cloud_candidate(
    *,
    db_overlay: dict[str, Any] | None,
    backend_instance_id: str | None = None,
    secret_revision: int = 1,
) -> tuple[MainCloudBackend, MainCloudBackendInstanceAuthority]:
    """Build and remotely attest one config before any DB activation."""

    settings = load_main_cloud_config(db_overlay=db_overlay)
    secret_refs = main_cloud_secret_references(settings.backend_id, db_overlay)
    backend = build_backend_from_config(settings)
    try:
        initialized = await backend.ensure_initialized()
        proof = backend.installation_proof_sha256
        if not initialized or proof is None:
            raise RuntimeError(
                "main-cloud backend did not attest a stable installation identity"
            )
        authority = MainCloudBackendInstanceAuthority.capture(
            backend_instance_id=backend_instance_id or str(uuid4()),
            backend_id=settings.backend_id,
            routing=main_cloud_routing_snapshot(settings),
            installation_proof_sha256=proof,
            secret_refs=secret_refs,
            secret_revision=secret_revision,
        )
        return backend, authority
    except BaseException:
        try:
            await backend.close()
        except Exception:
            pass
        raise


async def reload_active_main_cloud_instance(
    db: Any,
    router: MainCloudRouter,
    *,
    force_rebuild: bool = False,
) -> bool | None:
    """Install the exact current DB pointer after a post-build reread.

    ``None`` means no durable active instance exists. ``False`` means the
    pointer changed while the candidate was being built; the caller should
    retry or wait for the next notification. No stale candidate is activated.
    """

    active = await db.get_active_main_cloud_backend_instance()
    before = _active_coordinates(active)
    if before is None:
        return None
    authority = active["authority"]
    backend = await router.resolve_backend_instance(
        authority,
        force_rebuild=force_rebuild,
    )
    current = await db.get_active_main_cloud_backend_instance()
    if _active_coordinates(current) != before:
        logger.info(
            "Main-cloud active instance changed during adapter attestation; "
            "discarding stale activation"
        )
        return False
    await router.replace_active(backend, authority=authority)
    return True


async def preload_retained_main_cloud_instances(
    db: Any,
    router: MainCloudRouter,
) -> dict[str, str]:
    """Rebuild every retained adapter cache entry without fallback.

    One unavailable historical installation must not relabel its resources to
    the active backend. It remains absent from the cache and callers receive a
    typed refusal; the returned mapping is safe diagnostic state.
    """

    failures: dict[str, str] = {}
    authorities = await db.list_main_cloud_backend_instances()
    for authority in authorities:
        try:
            await router.resolve_backend_instance(authority)
        except Exception as exc:
            failures[authority.backend_instance_id] = type(exc).__name__
            logger.warning(
                "Retained main-cloud instance %s is unresolved (%s)",
                authority.backend_instance_id,
                type(exc).__name__,
            )
    return failures


async def initialize_main_cloud_instance_authority(
    db: Any,
    router: MainCloudRouter,
    *,
    legacy_overlay: dict[str, Any] | None,
    activated_by: str = "orchestrator-startup",
) -> dict[str, Any]:
    """Resolve existing authority or transactionally adopt first-boot config."""

    active = await db.get_active_main_cloud_backend_instance()
    if _active_coordinates(active) is not None:
        loaded = await reload_active_main_cloud_instance(db, router)
        if loaded is not True:
            raise RuntimeError("main-cloud active instance changed during startup")
        return active

    backend, proposed = await build_attested_main_cloud_candidate(
        db_overlay=legacy_overlay,
    )
    try:
        installed = await db.install_initial_main_cloud_backend_instance(
            proposed,
            activated_by=activated_by,
        )
        if _active_coordinates(installed) is None:
            # A racing replica may have installed a different valid instance.
            # Never relabel this candidate: close it and resolve the winner.
            await backend.close()
            loaded = await reload_active_main_cloud_instance(db, router)
            if loaded is not True:
                raise RuntimeError(
                    "main-cloud initial instance adoption lost authority"
                )
            winner = await db.get_active_main_cloud_backend_instance()
            if _active_coordinates(winner) is None:
                raise RuntimeError("main-cloud active instance is unavailable")
            return winner
        adopted = installed["authority"]
        if (
            adopted.backend_id != proposed.backend_id
            or adopted.routing != proposed.routing
            or adopted.installation_proof_sha256 != proposed.installation_proof_sha256
            or adopted.secret_refs != proposed.secret_refs
            or adopted.secret_revision != proposed.secret_revision
        ):
            raise RuntimeError("main-cloud initial instance adoption changed authority")
        await router.replace_active(backend, authority=adopted)
        return installed
    except BaseException:
        if backend is not router.active:
            try:
                await backend.close()
            except Exception:
                pass
        raise


async def activate_main_cloud_config(
    db: Any,
    router: MainCloudRouter,
    *,
    db_overlay: dict[str, Any] | None,
    expected_activation_revision: int,
    activated_by: str,
) -> dict[str, Any] | None:
    """Attest and CAS-activate an admin-proposed config.

    A routing or installation change creates a new immutable UUID. A change to
    secret references for the same proven installation rotates only the exact
    instance's secret revision. No unresolved adapter is installed locally.
    """

    current = await db.get_active_main_cloud_backend_instance()
    current_coordinates = _active_coordinates(current)
    if current_coordinates is None:
        if expected_activation_revision != 0:
            return None
        backend, candidate = await build_attested_main_cloud_candidate(
            db_overlay=db_overlay,
        )
        try:
            installed = await db.install_initial_main_cloud_backend_instance(
                candidate,
                activated_by=activated_by,
            )
            installed_coordinates = _active_coordinates(installed)
            if installed_coordinates is None:
                return None
            authority = installed["authority"]
            if (
                authority.backend_id != candidate.backend_id
                or authority.routing != candidate.routing
                or authority.installation_proof_sha256
                != candidate.installation_proof_sha256
                or authority.secret_refs != candidate.secret_refs
            ):
                return None
            current_after = await db.get_active_main_cloud_backend_instance()
            if _active_coordinates(current_after) != installed_coordinates:
                return None
            await router.replace_active(backend, authority=authority)
            return installed
        finally:
            if backend is not router.active:
                try:
                    await backend.close()
                except Exception:
                    pass
    if current_coordinates[1] != expected_activation_revision:
        return None
    current_authority = current["authority"]
    backend, candidate = await build_attested_main_cloud_candidate(
        db_overlay=db_overlay,
    )
    installed: dict[str, Any] | None = None
    authority: MainCloudBackendInstanceAuthority | None = None
    try:
        same_installation = (
            candidate.backend_id == current_authority.backend_id
            and candidate.routing == current_authority.routing
            and candidate.installation_proof_sha256
            == current_authority.installation_proof_sha256
        )
        if same_installation:
            if candidate.secret_refs == current_authority.secret_refs:
                authority = current_authority
                installed = current
            else:
                authority = MainCloudBackendInstanceAuthority.capture(
                    backend_instance_id=current_authority.backend_instance_id,
                    backend_id=current_authority.backend_id,
                    routing=current_authority.routing,
                    installation_proof_sha256=(
                        current_authority.installation_proof_sha256
                    ),
                    secret_refs=candidate.secret_refs,
                    secret_revision=current_authority.secret_revision + 1,
                )
                authority = await db.rotate_main_cloud_backend_secret_refs(
                    authority,
                    expected_secret_revision=current_authority.secret_revision,
                )
                if authority is None:
                    return None
                # The active pointer does not rotate for a reference-only
                # change, but its adapter revision does.
                installed = {
                    "authority": authority,
                    "activation_revision": expected_activation_revision,
                }
        else:
            authority = await db.register_main_cloud_backend_instance(candidate)
            if authority is None:
                return None
            installed = await db.activate_main_cloud_backend_instance(
                authority.backend_instance_id,
                expected_activation_revision=expected_activation_revision,
                activated_by=activated_by,
            )
            if _active_coordinates(installed) is None:
                return None

        assert authority is not None
        current_after = await db.get_active_main_cloud_backend_instance()
        expected_after = _active_coordinates(installed)
        if (
            expected_after is None
            or _active_coordinates(current_after) != expected_after
        ):
            return None
        await router.replace_active(backend, authority=authority)
        return installed
    finally:
        if backend is not router.active:
            try:
                await backend.close()
            except Exception:
                pass


__all__ = [
    "activate_main_cloud_config",
    "build_attested_main_cloud_candidate",
    "initialize_main_cloud_instance_authority",
    "preload_retained_main_cloud_instances",
    "reload_active_main_cloud_instance",
]
