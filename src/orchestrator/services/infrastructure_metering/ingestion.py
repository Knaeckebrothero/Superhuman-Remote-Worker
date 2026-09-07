"""Authenticated orchestration service for dedicated inventory collectors.

This is an app-DB shadow path only. It owns scope creation, grant issuance,
strict wire-to-store projection, and typed Pod/PVC/PV lifecycle hooks; it cannot
publish audit usage events or change a durable publication cutover state.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
import logging
import re
from typing import Any, Literal, Mapping, TypeVar, cast
import asyncpg
from pydantic import BaseModel
from starlette.requests import Request

from orchestrator.services.infrastructure_metering.config import (
    InfrastructureMeteringSettings,
)
from orchestrator.services.infrastructure_metering.agent_intervals import (
    AgentPodIntervalReconciler,
    classify_product_pod,
)
from orchestrator.services.infrastructure_metering.compute_activation import (
    ComputeActivation,
    confirm_compute_authority_snapshot,
)
from orchestrator.services.infrastructure_metering.ingestion_http import (
    AuthenticatedIngestionModel,
    IngestionRequestError,
    authenticate_ingestion_model,
)
from orchestrator.services.infrastructure_metering.ide_intervals import (
    IdePodIntervalReconciler,
)
from orchestrator.services.infrastructure_metering.ingestion_types import (
    InventoryItemWire,
    InventoryScopeWire,
    InventorySnapshotBegin,
    InventorySnapshotFinalize,
    InventorySnapshotItemBatch,
    InventoryTicketRequest,
    InventoryTicketResponse,
    InventoryWatchApply,
    InventoryWatchFinish,
    SNAPSHOT_BATCH_BODY_LIMIT,
    SNAPSHOT_METADATA_BODY_LIMIT,
    TICKET_BODY_LIMIT,
    WATCH_BODY_LIMIT,
    validate_normalized_pod_payload,
    validate_normalized_pvc_payload,
    validate_normalized_pv_payload,
    validate_normalized_vmi_payload,
)
from orchestrator.services.infrastructure_metering.inventory import (
    InventoryConflictError,
    InventoryContractError,
    InventoryFenceError,
    InventoryItem,
    InventoryRecoveryRequired,
    InventoryScopeIdentity,
    InventoryStore,
    InventoryTicketError,
    SanitizedInventoryError,
    SnapshotFinalization,
    TransportNonceClaim,
    WatchEventKind,
    WatchObjectEvent,
    canonical_request_digest,
)
from orchestrator.services.infrastructure_metering.pod_intervals import (
    PodIntervalReconciler,
)
from orchestrator.services.infrastructure_metering.storage_intervals import (
    StorageIntervalReconciler,
)
from orchestrator.services.infrastructure_metering.transport import COLLECTOR_HEADER
from orchestrator.services.infrastructure_metering.vmi_intervals import (
    VMIIntervalReconciler,
)


_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_M = TypeVar("_M", bound=BaseModel)

logger = logging.getLogger(__name__)

_CLEANUP_BATCH_LIMIT = 1_000
_MAX_CLEANUP_PASSES = 10

_POD_API_RESOURCE = "core/v1/pods"
_PVC_API_RESOURCE = "core/v1/persistentvolumeclaims"
_PV_API_RESOURCE = "core/v1/persistentvolumes"
_VMI_API_RESOURCE = "kubevirt.io/v1/virtualmachineinstances"
_VM_COLLECTOR_ID = "kubevirt-vmis"
_VM_STORAGE_COLLECTOR_ID = "kubevirt-storage"

IngestionOperation = Literal[
    "ticket",
    "snapshot_begin",
    "snapshot_items",
    "snapshot_finalize",
    "watch_apply",
    "watch_finish",
]


class InfrastructureIngestionService:
    """Fail-closed HTTP-to-transaction adapter for one stable installation."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        store: InventoryStore,
        settings: InfrastructureMeteringSettings,
        *,
        ingestion_key: str | bytes,
        collector_id: str = "kubernetes-pods",
        additional_ingestion_keys: Mapping[str, str | bytes] | None = None,
        compute_activation_overrides: Mapping[str, ComputeActivation] | None = None,
    ) -> None:
        settings.validate()
        if not settings.collector_enabled:
            raise ValueError("collector service requires its feature gate")
        key = (
            ingestion_key.encode()
            if isinstance(ingestion_key, str)
            else bytes(ingestion_key)
        )
        if len(key) < 32:
            raise ValueError("infrastructure metering ingestion key must be 32+ bytes")
        ingestion_keys = {collector_id: key}
        for additional_id, additional_key in (additional_ingestion_keys or {}).items():
            if (
                not isinstance(additional_id, str)
                or not additional_id
                or len(additional_id) > 128
                or additional_id in ingestion_keys
            ):
                raise ValueError("additional metering collector id is invalid")
            encoded = (
                additional_key.encode()
                if isinstance(additional_key, str)
                else bytes(additional_key)
            )
            if len(encoded) < 32:
                raise ValueError(
                    "infrastructure metering ingestion key must be 32+ bytes"
                )
            if encoded in ingestion_keys.values():
                raise ValueError("metering collector keys must be distinct")
            ingestion_keys[additional_id] = encoded
        self._pool = pool
        self.store = store
        self.settings = settings
        self._ingestion_keys = ingestion_keys
        self._collector_id = collector_id
        activation_overrides = dict(compute_activation_overrides or {})
        if set(activation_overrides) - {
            "agent_pod",
            "ide_workspace_pod",
            "workspace_vm",
        } or any(
            activation.activation_key != key
            for key, activation in activation_overrides.items()
        ):
            raise ValueError("compute activation override identity is invalid")
        self._pod_reconciler = PodIntervalReconciler(
            shadow_enabled=settings.shadow_enabled
        )
        self._agent_pod_reconciler = AgentPodIntervalReconciler(
            shadow_enabled=settings.agent_pod_shadow_enabled,
            activation=activation_overrides.get("agent_pod"),
        )
        self._ide_pod_reconciler = IdePodIntervalReconciler(
            shadow_enabled=settings.ide_pod_shadow_enabled,
            activation=activation_overrides.get("ide_workspace_pod"),
        )
        self._vmi_reconciler = VMIIntervalReconciler(
            shadow_enabled=settings.vm_shadow_enabled,
            activation=activation_overrides.get("workspace_vm"),
            max_lifecycle_clock_skew=getattr(
                store,
                "max_collector_clock_skew",
                timedelta(minutes=5),
            ),
        )
        self._storage_reconciler = StorageIntervalReconciler(
            shadow_enabled=(settings.pvc_shadow_enabled or settings.pv_shadow_enabled)
        )
        self._vm_storage_reconciler = StorageIntervalReconciler(
            shadow_enabled=(
                settings.vm_pvc_shadow_enabled or settings.vm_pv_shadow_enabled
            ),
        )

    async def _apply_pod_snapshot(self, conn: Any, context: Any, item: Any) -> Any:
        classification = classify_product_pod(item)
        if classification.is_agent:
            return await self._agent_pod_reconciler.apply_snapshot(conn, context, item)
        if classification.activation_key == "ide_workspace_pod":
            return await self._ide_pod_reconciler.apply_snapshot(conn, context, item)
        return await self._pod_reconciler.apply_snapshot(conn, context, item)

    async def _observe_pod_snapshot(self, conn: Any, context: Any, item: Any) -> None:
        # The Slice 1 comparison remains one-per-Pod and authoritative for the
        # existing workspace cutover. Slice 3 writes its independent per-class
        # proof beside it without changing that legacy count contract.
        await self._pod_reconciler.observe_snapshot(conn, context, item)
        if self.settings.agent_pod_shadow_enabled:
            await self._agent_pod_reconciler.observe_snapshot(conn, context, item)
        if self.settings.ide_pod_shadow_enabled:
            await self._ide_pod_reconciler.observe_snapshot(conn, context, item)

    async def _complete_pod_snapshot(self, conn: Any, context: Any) -> None:
        keys: list[str] = []
        if self.settings.agent_pod_shadow_enabled:
            keys.append("agent_pod")
        if self.settings.ide_pod_shadow_enabled:
            keys.append("ide_workspace_pod")
        await confirm_compute_authority_snapshot(
            conn,
            activation_keys=tuple(keys),
            snapshot_id=context.snapshot_id,
            scope_epoch_id=context.scope_epoch_id,
            inventory_scope_id=context.inventory_scope_id,
            received_at=context.received_at,
        )

    async def _complete_vmi_snapshot(self, conn: Any, context: Any) -> None:
        await confirm_compute_authority_snapshot(
            conn,
            activation_keys=("workspace_vm",),
            snapshot_id=context.snapshot_id,
            scope_epoch_id=context.scope_epoch_id,
            inventory_scope_id=context.inventory_scope_id,
            received_at=context.received_at,
        )

    async def _apply_pod_watch(self, conn: Any, context: Any, item: Any) -> Any:
        classification = classify_product_pod(item)
        if classification.is_agent:
            return await self._agent_pod_reconciler.apply_watch(conn, context, item)
        if classification.activation_key == "ide_workspace_pod":
            return await self._ide_pod_reconciler.apply_watch(conn, context, item)
        return await self._pod_reconciler.apply_watch(conn, context, item)

    async def _authenticated(
        self,
        request: Request,
        model_type: type[_M],
        *,
        request_kind: str,
        maximum_bytes: int,
    ) -> tuple[_M, AuthenticatedIngestionModel]:
        claimed_collector_id = request.headers.get(COLLECTOR_HEADER, "").strip()
        key = self._ingestion_keys.get(claimed_collector_id)
        if key is None:
            raise IngestionRequestError(401, "invalid collector authentication")
        authenticated = await authenticate_ingestion_model(
            request,
            key=key,
            model_type=model_type,
            request_kind=request_kind,
            maximum_bytes=maximum_bytes,
        )
        if authenticated.collector_id not in self._ingestion_keys:
            raise IngestionRequestError(401, "invalid collector authentication")
        return cast(_M, authenticated.model), authenticated

    def _scope(
        self, wire: InventoryScopeWire, collector_id: str
    ) -> InventoryScopeIdentity:
        allowed = False
        primary_collector_id = getattr(self, "_collector_id", "kubernetes-pods")
        if (
            collector_id == primary_collector_id
            and wire.source_cluster == self.settings.stable_cluster_id
        ):
            if wire.api_resource == _POD_API_RESOURCE:
                allowed = (
                    not wire.cluster_scoped
                    and wire.namespace in self.settings.namespace_allowlist
                )
            elif wire.api_resource == _PVC_API_RESOURCE:
                allowed = (
                    self.settings.pvc_inventory_enabled
                    and not wire.cluster_scoped
                    and wire.namespace in self.settings.namespace_allowlist
                )
            elif wire.api_resource == _PV_API_RESOURCE:
                allowed = (
                    self.settings.pv_inventory_enabled
                    and wire.cluster_scoped
                    and wire.namespace is None
                )
        elif (
            collector_id == _VM_COLLECTOR_ID
            and self.settings.vm_inventory_enabled
            and wire.source_cluster == self.settings.vm_stable_cluster_id
        ):
            allowed = (
                wire.api_resource == _VMI_API_RESOURCE
                and not wire.cluster_scoped
                and wire.namespace == self.settings.vm_namespace
            )
        elif (
            collector_id == _VM_STORAGE_COLLECTOR_ID
            and wire.source_cluster == self.settings.vm_stable_cluster_id
        ):
            if wire.api_resource == _PVC_API_RESOURCE:
                allowed = (
                    self.settings.vm_pvc_inventory_enabled
                    and not wire.cluster_scoped
                    and wire.namespace == self.settings.vm_namespace
                )
            elif wire.api_resource == _PV_API_RESOURCE:
                allowed = (
                    self.settings.vm_pv_inventory_enabled
                    and self.settings.vm_pv_cluster_wide_rbac_acknowledged
                    and wire.cluster_scoped
                    and wire.namespace is None
                )
        if not allowed:
            raise IngestionRequestError(403, "collector scope is not allowed")
        return InventoryScopeIdentity(
            collector_id=collector_id,
            source_cluster=wire.source_cluster,
            api_resource=wire.api_resource,
            namespace=wire.namespace,
        )

    def _scope_shadow_enabled(self, scope: InventoryScopeIdentity) -> bool:
        if scope.api_resource == _POD_API_RESOURCE:
            return self.settings.shadow_enabled
        if scope.api_resource == _PVC_API_RESOURCE:
            if scope.collector_id == _VM_STORAGE_COLLECTOR_ID:
                return self.settings.vm_pvc_shadow_enabled
            return self.settings.pvc_shadow_enabled
        if scope.api_resource == _PV_API_RESOURCE:
            if scope.collector_id == _VM_STORAGE_COLLECTOR_ID:
                return self.settings.vm_pv_shadow_enabled
            return self.settings.pv_shadow_enabled
        if scope.api_resource == _VMI_API_RESOURCE:
            return self.settings.vm_shadow_enabled
        return False

    def _storage_reconciler_for_scope(
        self, scope: InventoryScopeIdentity
    ) -> StorageIntervalReconciler:
        if scope.collector_id == _VM_STORAGE_COLLECTOR_ID:
            return self._vm_storage_reconciler
        return self._storage_reconciler

    @staticmethod
    def _scope_interval_mutations_enabled(scope: InventoryScopeIdentity) -> bool:
        # Every storage authority passes through the database-owned per-source
        # activation guard. A remote source in disabled/shadow state returns no
        # interval mutation; it can never inherit another cluster's active
        # basis. Keeping reconciliation enabled here is necessary for the same
        # code path to begin lifecycle accrual after its own scheduled boundary.
        return True

    async def _ensure_scope_epoch(
        self, scope: InventoryScopeIdentity
    ) -> asyncpg.Record:
        """Create or resolve the active epoch under the current DB generation."""

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                generation = int(
                    await conn.fetchval(
                        "SELECT leader_generation FROM infra_metering_control "
                        "WHERE singleton = TRUE FOR SHARE"
                    )
                    or 0
                )
                if generation <= 0:
                    raise IngestionRequestError(503, "metering leader is unavailable")
                await conn.execute(
                    "INSERT INTO resource_inventory_scopes ("
                    "collector_id, source_cluster, api_resource, namespace) "
                    "VALUES ($1, $2, $3, $4) "
                    "ON CONFLICT (collector_id, source_cluster, api_resource, "
                    "namespace) DO NOTHING",
                    scope.collector_id,
                    scope.source_cluster,
                    scope.api_resource,
                    scope.namespace,
                )
                scope_row = await conn.fetchrow(
                    "SELECT id FROM resource_inventory_scopes "
                    "WHERE collector_id=$1 AND source_cluster=$2 "
                    "AND api_resource=$3 AND namespace IS NOT DISTINCT FROM $4 "
                    "FOR UPDATE",
                    scope.collector_id,
                    scope.source_cluster,
                    scope.api_resource,
                    scope.namespace,
                )
                if scope_row is None:
                    raise InventoryContractError("inventory scope upsert failed")
                epoch = await conn.fetchrow(
                    "SELECT * FROM resource_inventory_scope_epochs "
                    "WHERE scope_id=$1 AND retired_at IS NULL FOR UPDATE",
                    scope_row["id"],
                )
                if epoch is None:
                    next_number = await conn.fetchval(
                        "SELECT COALESCE(max(epoch_number), 0) + 1 "
                        "FROM resource_inventory_scope_epochs WHERE scope_id=$1",
                        scope_row["id"],
                    )
                    epoch_id = await conn.fetchval(
                        "INSERT INTO resource_inventory_scope_epochs ("
                        "scope_id, epoch_number, coverage_mode, "
                        "leader_generation) VALUES ($1, $2, 'list-watch', $3) "
                        "RETURNING id",
                        scope_row["id"],
                        next_number,
                        generation,
                    )
                    epoch = await conn.fetchrow(
                        "SELECT * FROM resource_inventory_scope_epochs "
                        "WHERE id=$1 FOR UPDATE",
                        epoch_id,
                    )
                if epoch is None:
                    raise InventoryContractError("inventory scope epoch is unavailable")
                return epoch

    async def _require_generation(self, generation: int) -> None:
        current = await self._pool.fetchval(
            "SELECT leader_generation FROM infra_metering_control "
            "WHERE singleton = TRUE"
        )
        if int(current or -1) != generation:
            raise IngestionRequestError(409, "metering generation is stale")

    @staticmethod
    def _item_error_code(item: InventoryItemWire) -> str:
        value = item.normalized.get("normalization_error")
        if isinstance(value, str) and _SAFE_ERROR_CODE.fullmatch(value):
            return value
        diagnostics = item.normalized.get("diagnostics")
        if isinstance(diagnostics, list):
            for diagnostic in diagnostics:
                if not isinstance(diagnostic, dict):
                    continue
                value = diagnostic.get("code")
                if isinstance(value, str) and _SAFE_ERROR_CODE.fullmatch(value):
                    return value
        return "invalid-item"

    @classmethod
    def _inventory_item(cls, item: InventoryItemWire) -> InventoryItem:
        normalized = item.normalized
        try:
            if item.scope.api_resource == _POD_API_RESOURCE:
                validate_normalized_pod_payload(normalized)
                expected_kind = "pod"
            elif item.scope.api_resource == _PVC_API_RESOURCE:
                validate_normalized_pvc_payload(normalized)
                expected_kind = "pvc"
            elif item.scope.api_resource == _PV_API_RESOURCE:
                validate_normalized_pv_payload(normalized)
                expected_kind = "volume"
            elif item.scope.api_resource == _VMI_API_RESOURCE:
                validate_normalized_vmi_payload(normalized)
                expected_kind = "vmi"
            else:
                raise ValueError("unsupported inventory API resource")
        except ValueError as exc:
            raise IngestionRequestError(
                400, "normalized inventory payload is invalid"
            ) from exc
        if (
            item.kind != expected_kind
            or normalized.get("uid") != item.uid
            or normalized.get("namespace") != item.scope.namespace
            or normalized.get("valid_for_metering") is not item.valid_for_metering
            or normalized.get("revision_hash") != item.revision_hash
        ):
            raise IngestionRequestError(400, "normalized item identity mismatch")
        source_kind = normalized.get("source_kind")
        if source_kind != item.kind:
            raise IngestionRequestError(400, "normalized item identity mismatch")
        return InventoryItem(
            source_kind=item.kind,
            source_uid=item.uid,
            revision_hash=item.revision_hash,
            normalized_item=normalized,
            valid_for_metering=item.valid_for_metering,
            item_error=(
                None
                if item.valid_for_metering
                else SanitizedInventoryError(code=cls._item_error_code(item))
            ),
        )

    async def ticket(self, request: Request) -> dict[str, Any]:
        model, authenticated = await self._authenticated(
            request,
            InventoryTicketRequest,
            request_kind="snapshot-ticket",
            maximum_bytes=TICKET_BODY_LIMIT,
        )
        scope = self._scope(model.scope, authenticated.collector_id)
        epoch = await self._ensure_scope_epoch(scope)
        claim = authenticated.transport_claim
        if model.intent == "watch-session":
            if not self._scope_shadow_enabled(scope):
                raise IngestionRequestError(403, "watch ingestion is not enabled")
            generation = int(
                await self._pool.fetchval(
                    "SELECT leader_generation FROM infra_metering_control "
                    "WHERE singleton = TRUE"
                )
                or 0
            )
            fresh_list = (
                generation > 0
                and int(epoch["leader_generation"]) == generation
                and epoch["last_complete_snapshot_id"] is not None
                and await self._pool.fetchval(
                    "SELECT TRUE FROM resource_inventory_snapshots "
                    "WHERE id=$1 AND scope_epoch_id=$2 AND leader_generation=$3 "
                    "AND complete AND manifest_state='sealed'",
                    epoch["last_complete_snapshot_id"],
                    epoch["id"],
                    generation,
                )
            )
            if not fresh_list:
                raise IngestionRequestError(409, "fresh inventory list is required")
            claim = replace(claim, request_kind="watch-session")
            grant = await self.store.issue_watch_session(
                epoch["id"],
                canonical_request_digest(authenticated.body),
                model.starting_resource_version or "",
                scope=scope,
                transport=claim,
            )
            response = InventoryTicketResponse(
                ticket_id=grant.id,
                ticket_token=grant.token,
                leader_generation=grant.leader_generation,
                expires_at=grant.expires_at,
                last_resource_version=grant.starting_resource_version,
            )
            return response.model_dump(mode="json")

        if epoch["continuity_health"] == "gap":
            recovery_claim = replace(claim, request_kind="scope-recovery")
            await self.store.start_watch_recovery_epoch(
                epoch["id"], scope=scope, transport=recovery_claim
            )
            # The recovery transition consumed this signed nonce. A normal
            # retry obtains a fresh signature and binds its ticket to the new
            # epoch without conflating both side effects.
            raise IngestionRequestError(409, "inventory recovery started; retry")
        is_vm_scope = scope.api_resource == _VMI_API_RESOURCE
        if is_vm_scope != (model.controller_epoch is not None):
            raise IngestionRequestError(
                400, "collector controller identity does not match its scope"
            )
        if is_vm_scope and epoch["controller_epoch"] is not None:
            if model.controller_epoch != epoch["controller_epoch"]:
                recovery_claim = replace(claim, request_kind="controller-epoch-change")
                await self.store.start_controller_epoch_recovery(
                    epoch["id"], scope=scope, transport=recovery_claim
                )
                raise IngestionRequestError(
                    409, "collector controller recovery started; retry"
                )
            if model.sequence is None or model.sequence <= epoch["last_sequence"]:
                raise IngestionRequestError(409, "collector sequence is stale")
        grant = await self.store.issue_ingest_ticket(
            epoch["id"],
            canonical_request_digest(authenticated.body),
            scope=scope,
            transport=claim,
            require_healthy_continuity=True,
            ttl=timedelta(seconds=self.settings.ingestion_ticket_ttl_seconds),
            max_snapshot_items=self.settings.max_snapshot_items,
            max_snapshot_bytes=self.settings.max_snapshot_bytes,
        )
        response = InventoryTicketResponse(
            ticket_id=grant.id,
            ticket_token=grant.token,
            leader_generation=grant.leader_generation,
            expires_at=grant.expires_at,
            last_resource_version=epoch["last_resource_version"],
        )
        return response.model_dump(mode="json")

    async def snapshot_begin(self, request: Request) -> dict[str, Any]:
        model, authenticated = await self._authenticated(
            request,
            InventorySnapshotBegin,
            request_kind="snapshot-begin",
            maximum_bytes=SNAPSHOT_METADATA_BODY_LIMIT,
        )
        if model.collector_id != authenticated.collector_id:
            raise IngestionRequestError(403, "collector identity mismatch")
        scope = self._scope(model.scope, authenticated.collector_id)
        handle = await self.store.begin_snapshot(
            model.ticket_token,
            model.ticket_id,
            model.snapshot_id,
            model.collection_started_at,
            scope=scope,
            transport=authenticated.transport_claim,
            controller_epoch=model.controller_epoch,
            sequence=model.sequence,
        )
        return {
            "snapshot_id": str(handle.snapshot_id),
            "leader_generation": handle.leader_generation,
            "replayed": handle.replayed,
        }

    async def snapshot_items(self, request: Request) -> dict[str, Any]:
        model, authenticated = await self._authenticated(
            request,
            InventorySnapshotItemBatch,
            request_kind="snapshot-items",
            maximum_bytes=SNAPSHOT_BATCH_BODY_LIMIT,
        )
        scope = self._scope(model.scope, authenticated.collector_id)
        items = [self._inventory_item(item) for item in model.items]
        result = await self.store.stage_items(
            model.ticket_token,
            model.ticket_id,
            model.snapshot_id,
            items,
            scope=scope,
            transport=authenticated.transport_claim,
        )
        return {"inserted": result.inserted, "total": result.total}

    async def snapshot_finalize(self, request: Request) -> dict[str, Any]:
        model, authenticated = await self._authenticated(
            request,
            InventorySnapshotFinalize,
            request_kind="snapshot-finalize",
            maximum_bytes=SNAPSHOT_METADATA_BODY_LIMIT,
        )
        scope = self._scope(model.scope, authenticated.collector_id)
        scope_shadow_enabled = self._scope_shadow_enabled(scope)
        if model.shadow_enabled != scope_shadow_enabled:
            raise IngestionRequestError(
                409, "collector shadow mode does not match the server"
            )
        finalization = SnapshotFinalization(
            collection_completed_at=model.collection_completed_at,
            source_snapshot_at=model.source_snapshot_at,
            complete=model.complete,
            resource_version=model.resource_version,
            item_count=model.item_count,
            item_digest=model.item_digest,
            controller_epoch=model.controller_epoch,
            sequence=model.sequence,
            fatal_errors=tuple(
                SanitizedInventoryError(code=error.error_class)
                for error in model.fatal_errors
            ),
        )
        interval_mutator = None
        observation_hook = None
        completion_hook = None
        absence_mutator = None
        if scope_shadow_enabled:
            if scope.api_resource == _POD_API_RESOURCE:
                if (
                    self.settings.agent_pod_shadow_enabled
                    or self.settings.ide_pod_shadow_enabled
                ):
                    interval_mutator = self._apply_pod_snapshot
                    observation_hook = self._observe_pod_snapshot
                    completion_hook = self._complete_pod_snapshot
                    if self.settings.agent_pod_shadow_enabled:
                        absence_mutator = self._agent_pod_reconciler.apply_absence
                else:
                    interval_mutator = self._pod_reconciler.apply_snapshot
                    observation_hook = self._pod_reconciler.observe_snapshot
            elif scope.api_resource == _PVC_API_RESOURCE:
                storage_reconciler = self._storage_reconciler_for_scope(scope)
                observation_hook = storage_reconciler.observe_snapshot
                if self._scope_interval_mutations_enabled(scope):
                    interval_mutator = storage_reconciler.apply_snapshot
            elif scope.api_resource == _PV_API_RESOURCE:
                storage_reconciler = self._storage_reconciler_for_scope(scope)
                observation_hook = storage_reconciler.observe_snapshot
                completion_hook = storage_reconciler.complete_snapshot
                if self._scope_interval_mutations_enabled(scope):
                    interval_mutator = storage_reconciler.apply_snapshot
                    absence_mutator = storage_reconciler.apply_absence
            elif scope.api_resource == _VMI_API_RESOURCE:
                interval_mutator = self._vmi_reconciler.apply_snapshot
                observation_hook = self._vmi_reconciler.observe_snapshot
                completion_hook = self._complete_vmi_snapshot
        require_legacy_shadow_comparison = (
            scope_shadow_enabled and scope.api_resource != _VMI_API_RESOURCE
        )
        result = await self.store.finalize_snapshot(
            model.ticket_token,
            model.ticket_id,
            model.snapshot_id,
            finalization,
            scope=scope,
            transport=authenticated.transport_claim,
            interval_mutator=interval_mutator,
            observation_hook=observation_hook,
            completion_hook=completion_hook,
            absence_mutator=absence_mutator,
            require_shadow_comparison=require_legacy_shadow_comparison,
            reconcile_intervals=(
                scope_shadow_enabled and self._scope_interval_mutations_enabled(scope)
            ),
        )
        return {
            "snapshot_id": str(result.snapshot_id),
            "complete": result.complete,
            "present_items": result.present_items,
            "invalid_items": result.invalid_items,
            "confirmed_intervals": result.confirmed_intervals,
            "closed_intervals": result.closed_intervals,
            "pending_valid_items": result.pending_valid_items,
            "shadow_comparisons": result.shadow_comparisons,
            "replayed": result.replayed,
        }

    async def watch_apply(self, request: Request) -> dict[str, Any]:
        model, authenticated = await self._authenticated(
            request,
            InventoryWatchApply,
            request_kind="watch-event",
            maximum_bytes=WATCH_BODY_LIMIT,
        )
        await self._require_generation(model.leader_generation)
        observation = model.observation
        scope = self._scope(observation.scope, authenticated.collector_id)
        if not self._scope_shadow_enabled(scope):
            raise IngestionRequestError(403, "watch ingestion is not enabled")
        item = (
            None if observation.item is None else self._inventory_item(observation.item)
        )
        lifecycle = {} if item is None else item.normalized_item.get("lifecycle", {})
        terminal = isinstance(lifecycle, dict) and lifecycle.get("terminal") is True
        # Kubernetes' watch API uses uppercase wire event names, while the
        # durable inventory model stores canonical lowercase values.  Resolve
        # the already-Literal-validated wire value by enum member name rather
        # than trying to parse it as the enum's persisted value.
        event_kind = WatchEventKind[observation.event_type]
        event = WatchObjectEvent(
            event_type=event_kind,
            resource_version=observation.resource_version,
            collector_observed_at=observation.collector_observed_at,
            event_bytes=observation.source_event_bytes,
            item=(
                item
                if event_kind in {WatchEventKind.ADDED, WatchEventKind.MODIFIED}
                else None
            ),
            source_kind=(
                item.source_kind
                if event_kind is WatchEventKind.DELETED and item
                else None
            ),
            source_uid=(
                item.source_uid
                if event_kind is WatchEventKind.DELETED and item
                else None
            ),
            terminal=(
                terminal
                if event_kind in {WatchEventKind.ADDED, WatchEventKind.MODIFIED}
                else False
            ),
        )
        if scope.api_resource == _POD_API_RESOURCE:
            interval_mutator = (
                self._apply_pod_watch
                if (
                    self.settings.agent_pod_shadow_enabled
                    or self.settings.ide_pod_shadow_enabled
                )
                else self._pod_reconciler.apply_watch
            )
        elif scope.api_resource in {_PVC_API_RESOURCE, _PV_API_RESOURCE}:
            storage_reconciler = self._storage_reconciler_for_scope(scope)
            interval_mutator = storage_reconciler.apply_watch
        elif scope.api_resource == _VMI_API_RESOURCE:
            interval_mutator = self._vmi_reconciler.apply_watch
        else:
            raise IngestionRequestError(
                409, "inventory lifecycle adapter is unavailable"
            )
        deletion_mutator = None
        terminal_mutator = None
        if (
            scope.api_resource == _POD_API_RESOURCE
            and self.settings.agent_pod_shadow_enabled
        ):
            deletion_mutator = self._agent_pod_reconciler.apply_deletion
            terminal_mutator = self._agent_pod_reconciler.apply_terminal
        elif scope.api_resource == _PV_API_RESOURCE:
            deletion_mutator = storage_reconciler.apply_deletion
        result = await self.store.apply_watch_event(
            model.ticket_token,
            model.ticket_id,
            model.event_id,
            authenticated.transport_claim.request_digest,
            model.expected_resource_version,
            event,
            scope=scope,
            transport=authenticated.transport_claim,
            interval_mutator=interval_mutator,
            deletion_mutator=deletion_mutator,
            terminal_mutator=terminal_mutator,
            reconcile_intervals=self._scope_interval_mutations_enabled(scope),
        )
        return {
            "event_id": str(result.event_id),
            "resource_version": result.resource_version,
            "mutation_action": str(result.mutation_action),
            "session_consumed": result.session_consumed,
            "replayed": result.replayed,
        }

    async def watch_finish(self, request: Request) -> dict[str, Any]:
        model, authenticated = await self._authenticated(
            request,
            InventoryWatchFinish,
            request_kind="watch-finish",
            maximum_bytes=WATCH_BODY_LIMIT,
        )
        await self._require_generation(model.leader_generation)
        scope = self._scope(model.scope, authenticated.collector_id)
        if not self._scope_shadow_enabled(scope):
            raise IngestionRequestError(403, "watch ingestion is not enabled")
        if model.history_lost:
            assert model.history_event_id is not None
            assert model.gap_reason is not None
            claim: TransportNonceClaim = replace(
                authenticated.transport_claim, request_kind="watch-history-lost"
            )
            result = await self.store.record_watch_gap(
                model.ticket_token,
                model.ticket_id,
                model.history_event_id,
                authenticated.transport_claim.request_digest,
                model.committed_resource_version,
                gap_reason=model.gap_reason.value,
                alternate_expected_resource_version=(model.ambiguous_resource_version),
                scope=scope,
                transport=claim,
                event_bytes=0,
                collector_observed_at=model.completed_at,
            )
            return {
                "history_lost": True,
                "coverage_gap_id": (
                    str(result.coverage_gap_id) if result.coverage_gap_id else None
                ),
                "replayed": result.replayed,
            }
        consumed = await self.store.finish_watch_session(
            model.ticket_token,
            model.ticket_id,
            scope=scope,
            transport=authenticated.transport_claim,
        )
        return {"history_lost": False, "consumed": consumed}


async def dispatch_ingestion_request(
    service: InfrastructureIngestionService | None,
    operation: IngestionOperation,
    request: Request,
) -> dict[str, Any]:
    """Call one fixed ingestion operation with a sanitized failure contract."""

    if service is None:
        raise IngestionRequestError(503, "infrastructure ingestion is unavailable")
    handler = cast(Any, getattr(service, operation))
    try:
        return cast(dict[str, Any], await handler(request))
    except IngestionRequestError:
        raise
    except InventoryContractError as exc:
        raise IngestionRequestError(400, "invalid inventory contract") from exc
    except InventoryFenceError as exc:
        raise IngestionRequestError(409, "inventory generation conflict") from exc
    except InventoryRecoveryRequired as exc:
        raise IngestionRequestError(409, "inventory recovery required") from exc
    except InventoryConflictError as exc:
        raise IngestionRequestError(409, "inventory state conflict") from exc
    except InventoryTicketError as exc:
        raise IngestionRequestError(409, "inventory grant unavailable") from exc
    except asyncpg.PostgresError as exc:
        raise IngestionRequestError(
            503, "infrastructure ingestion is unavailable"
        ) from exc


async def run_inventory_generation_loop(
    stop: asyncio.Event,
    store: InventoryStore,
    *,
    generation: int | None = None,
    cleanup_interval_seconds: float = 300.0,
    snapshot_item_retention: timedelta = timedelta(days=7),
    diagnostic_retention: timedelta = timedelta(days=35),
    abandoned_staging_retention: timedelta = timedelta(hours=24),
) -> None:
    """Own one database fencing generation for exactly one leader tenure.

    Cleanup is deliberately bounded per wake-up so a backlog cannot monopolize
    the singleton leader.  A full batch is drained again immediately, up to the
    pass cap; anything left is safely picked up on the next interval.
    """

    if cleanup_interval_seconds <= 0:
        raise ValueError("cleanup interval must be positive")
    generation = await store.activate_generation(generation)
    logger.info(
        "infrastructure inventory generation activated generation=%s", generation
    )
    try:
        while not stop.is_set():
            nonce_more = True
            diagnostics_more = True
            for _ in range(_MAX_CLEANUP_PASSES):
                if stop.is_set():
                    break
                if nonce_more:
                    try:
                        nonce_count = await store.purge_expired_transport_nonces(
                            limit=_CLEANUP_BATCH_LIMIT
                        )
                        nonce_more = nonce_count == _CLEANUP_BATCH_LIMIT
                    except Exception as exc:
                        nonce_more = False
                        logger.warning(
                            "infrastructure inventory nonce cleanup failed class=%s",
                            type(exc).__name__,
                        )
                if diagnostics_more:
                    try:
                        purge_result = await store.purge_diagnostics(
                            generation,
                            snapshot_item_retention=snapshot_item_retention,
                            diagnostic_retention=diagnostic_retention,
                            abandoned_staging_retention=(abandoned_staging_retention),
                            limit=_CLEANUP_BATCH_LIMIT,
                        )
                        diagnostics_more = purge_result.might_have_more
                    except Exception as exc:
                        diagnostics_more = False
                        logger.warning(
                            "infrastructure inventory diagnostic cleanup failed "
                            "class=%s",
                            type(exc).__name__,
                        )
                if not nonce_more and not diagnostics_more:
                    break
            try:
                await asyncio.wait_for(stop.wait(), timeout=cleanup_interval_seconds)
            except TimeoutError:
                pass
    finally:
        await store.deactivate_generation(generation)
        logger.info(
            "infrastructure inventory generation deactivated generation=%s",
            generation,
        )


__all__ = [
    "InfrastructureIngestionService",
    "dispatch_ingestion_request",
    "run_inventory_generation_loop",
]
